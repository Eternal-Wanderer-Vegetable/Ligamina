//! Core-generated container launches for Hosts that must not run directly on the host
//! machine (ADR 0003). The configuration only says which image, mounts and limits to
//! use; the hardening flags are always added here, and unsafe settings are rejected.

use std::{collections::BTreeMap, ffi::OsString, time::Duration};

use sha2::{Digest, Sha256};

use super::local::{CleanupCommand, SPAWN_PLACEHOLDER};

/// Label carrying the Host ID on every container the Core starts.
pub const HOST_LABEL: &str = "io.ligamina.host";
/// Label tying containers to one daemon instance (derived from its database path).
pub const OWNER_LABEL: &str = "io.ligamina.owner";
const CLEANUP_TIMEOUT: Duration = Duration::from_secs(30);
const PROTECTED_TARGETS: [&str; 4] = ["/", "/proc", "/sys", "/dev"];

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BindMount {
    pub source: String,
    pub target: String,
    pub read_only: bool,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Tmpfs {
    pub target: String,
    pub size_mb: u64,
    pub exec: bool,
}

/// What a Host's container should look like. Everything security-relevant that is not
/// listed here is fixed by [`container_args`].
#[derive(Clone, Debug, PartialEq)]
pub struct ContainerSpec {
    /// Command prefix for the container runtime, e.g. `["docker"]` or
    /// `["C:\\Windows\\System32\\wsl.exe", "-e", "docker"]`.
    pub docker: Vec<String>,
    pub image: String,
    pub entrypoint: Option<String>,
    pub command: Vec<String>,
    pub workdir: Option<String>,
    pub user: Option<String>,
    pub environment: BTreeMap<String, String>,
    pub mounts: Vec<BindMount>,
    pub tmpfs: Vec<Tmpfs>,
    pub memory_mb: u64,
    pub cpus: f64,
    pub pids_limit: u64,
    pub network: String,
}

/// Why a container specification was refused.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Rejection(pub String);

impl std::fmt::Display for Rejection {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.0)
    }
}

/// Checks a specification against the isolation rules. `allow_writable_mounts` is only
/// true for trusted Hosts; untrusted Hosts get read-only bind mounts and tmpfs only.
pub fn validate(
    host_id: &str,
    spec: &ContainerSpec,
    allow_writable_mounts: bool,
) -> Result<(), Rejection> {
    let reject = |rule: &str| Err(Rejection(format!("Host {host_id}: {rule}")));
    if container_name_part(host_id).is_none() {
        return reject("an isolated Host ID may only use letters, digits, `_`, `.` and `-`");
    }
    if spec.docker.is_empty() || spec.docker.iter().any(|part| part.is_empty()) {
        return reject("isolation.docker must be a non-empty command prefix");
    }
    if !is_digest_pinned(&spec.image) {
        return reject("isolation.image must be pinned by digest (name@sha256:<64 hex>)");
    }
    if spec.network != "none" {
        return reject("isolation.network must be `none`");
    }
    if spec.memory_mb == 0 || spec.pids_limit == 0 || !(spec.cpus.is_finite() && spec.cpus > 0.0) {
        return reject("isolation.memory_mb, cpus and pids_limit must be positive");
    }
    for mount in &spec.mounts {
        if mount.source.is_empty() || mount.source.contains(',') || mount.source.contains('=') {
            return reject("isolation.mounts[].source must be a non-empty path without `,` or `=`");
        }
        check_target(&mount.target)
            .or_else(|rule| reject(&format!("isolation.mounts[].target {rule}")))?;
        if !mount.read_only && !allow_writable_mounts {
            return reject("bind mounts of an untrusted Host must be read-only");
        }
    }
    for tmpfs in &spec.tmpfs {
        check_target(&tmpfs.target)
            .or_else(|rule| reject(&format!("isolation.tmpfs[].target {rule}")))?;
        if tmpfs.size_mb == 0 {
            return reject("isolation.tmpfs[].size_mb must be positive");
        }
    }
    for key in spec.environment.keys() {
        if key.is_empty() || key.contains('=') {
            return reject("isolation.environment keys must be non-empty and contain no `=`");
        }
    }
    for value in spec.workdir.iter() {
        check_target(value).or_else(|rule| reject(&format!("isolation.workdir {rule}")))?;
    }
    Ok(())
}

fn check_target(target: &str) -> Result<(), String> {
    if !target.starts_with('/') || target.contains(',') || target.contains(':') {
        return Err("must be an absolute container path without `,` or `:`".to_owned());
    }
    let trimmed = target.trim_end_matches('/');
    let normalized = if trimmed.is_empty() { "/" } else { trimmed };
    if PROTECTED_TARGETS.contains(&normalized) {
        return Err(format!("may not be `{normalized}`"));
    }
    Ok(())
}

fn is_digest_pinned(image: &str) -> bool {
    let Some((name, digest)) = image.rsplit_once("@sha256:") else {
        return false;
    };
    !name.is_empty() && digest.len() == 64 && digest.bytes().all(|byte| byte.is_ascii_hexdigit())
}

/// The Host ID as it appears in container names, if it is a valid name part.
fn container_name_part(host_id: &str) -> Option<&str> {
    let valid = !host_id.is_empty()
        && host_id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'.' | b'-'));
    valid.then_some(host_id)
}

/// Per-daemon owner value: containers from another daemon on the same machine are
/// never swept or removed.
pub fn owner_for(database_path: &str) -> String {
    let digest = Sha256::digest(database_path.as_bytes());
    digest
        .iter()
        .take(6)
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// Container name for one launch; contains [`SPAWN_PLACEHOLDER`].
pub fn container_name(host_id: &str) -> String {
    format!("{}-{host_id}-{SPAWN_PLACEHOLDER}", "ligamina")
}

/// `(program, args)` that start the Host's container. Call [`validate`] first.
pub fn container_args(
    host_id: &str,
    owner: &str,
    spec: &ContainerSpec,
) -> (OsString, Vec<OsString>) {
    let mut args: Vec<String> = spec.docker[1..].to_vec();
    args.extend([
        "run".to_owned(),
        "--rm".to_owned(),
        "-i".to_owned(),
        "--name".to_owned(),
        container_name(host_id),
        "--label".to_owned(),
        format!("{HOST_LABEL}={host_id}"),
        "--label".to_owned(),
        format!("{OWNER_LABEL}={owner}"),
        "--network".to_owned(),
        "none".to_owned(),
        "--read-only".to_owned(),
        "--cap-drop".to_owned(),
        "ALL".to_owned(),
        "--security-opt".to_owned(),
        "no-new-privileges".to_owned(),
        "--pids-limit".to_owned(),
        spec.pids_limit.to_string(),
        "--memory".to_owned(),
        format!("{}m", spec.memory_mb),
        "--cpus".to_owned(),
        format_cpus(spec.cpus),
    ]);
    if let Some(user) = &spec.user {
        args.extend(["--user".to_owned(), user.clone()]);
    }
    if let Some(workdir) = &spec.workdir {
        args.extend(["--workdir".to_owned(), workdir.clone()]);
    }
    for tmpfs in &spec.tmpfs {
        let exec = if tmpfs.exec { "exec" } else { "noexec" };
        args.extend([
            "--tmpfs".to_owned(),
            format!(
                "{}:rw,{exec},nosuid,nodev,size={}m,mode=1777",
                tmpfs.target, tmpfs.size_mb
            ),
        ]);
    }
    for mount in &spec.mounts {
        let mut value = format!("type=bind,src={},dst={}", mount.source, mount.target);
        if mount.read_only {
            value.push_str(",readonly");
        }
        args.extend(["--mount".to_owned(), value]);
    }
    for (key, value) in &spec.environment {
        args.extend(["-e".to_owned(), format!("{key}={value}")]);
    }
    if let Some(entrypoint) = &spec.entrypoint {
        args.extend(["--entrypoint".to_owned(), entrypoint.clone()]);
    }
    args.push(spec.image.clone());
    args.extend(spec.command.iter().cloned());
    (
        OsString::from(&spec.docker[0]),
        args.into_iter().map(OsString::from).collect(),
    )
}

fn format_cpus(cpus: f64) -> String {
    let text = format!("{cpus:.3}");
    text.trim_end_matches('0').trim_end_matches('.').to_owned()
}

/// Removes the launch's container by name (a no-op for an already removed container is
/// reported as a failure by docker; the supervisor runs it once per launch).
pub fn cleanup_command(host_id: &str, spec: &ContainerSpec) -> CleanupCommand {
    let mut args: Vec<OsString> = spec.docker[1..].iter().map(OsString::from).collect();
    args.extend(["rm", "-f"].map(OsString::from));
    args.push(OsString::from(container_name(host_id)));
    CleanupCommand {
        program: OsString::from(&spec.docker[0]),
        args,
        timeout: CLEANUP_TIMEOUT,
    }
}

/// Removes containers left by an earlier run of this daemon (for example after the Core
/// was killed). Returns how many were removed.
pub async fn sweep_leftovers(
    host_id: &str,
    owner: &str,
    spec: &ContainerSpec,
) -> Result<usize, String> {
    let mut list = tokio::process::Command::new(&spec.docker[0]);
    #[cfg(windows)]
    list.creation_flags(windows_sys::Win32::System::Threading::CREATE_NO_WINDOW);
    let output = tokio::time::timeout(
        CLEANUP_TIMEOUT,
        list.args(&spec.docker[1..])
            .args([
                "ps",
                "-aq",
                "--filter",
                &format!("label={HOST_LABEL}={host_id}"),
                "--filter",
                &format!("label={OWNER_LABEL}={owner}"),
            ])
            .stdin(std::process::Stdio::null())
            .kill_on_drop(true)
            .output(),
    )
    .await
    .map_err(|_| "listing leftover containers timed out".to_owned())?
    .map_err(|error| format!("could not list leftover containers: {error}"))?;
    if !output.status.success() {
        return Err(format!(
            "listing leftover containers failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    let ids: Vec<String> = String::from_utf8_lossy(&output.stdout)
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty() && line.bytes().all(|byte| byte.is_ascii_hexdigit()))
        .map(str::to_owned)
        .collect();
    if ids.is_empty() {
        return Ok(0);
    }
    let mut args: Vec<OsString> = spec.docker[1..].iter().map(OsString::from).collect();
    args.extend(["rm", "-f"].map(OsString::from));
    args.extend(ids.iter().map(OsString::from));
    super::local::run_cleanup(&CleanupCommand {
        program: OsString::from(&spec.docker[0]),
        args,
        timeout: CLEANUP_TIMEOUT,
    })
    .await?;
    Ok(ids.len())
}

#[cfg(test)]
mod tests {
    use super::*;

    const IMAGE: &str =
        "example/runtime@sha256:9c1280e40a0bfa7fb2a7135a58316624a9999ad5333ad66938eb363f7213b6b5";

    fn spec() -> ContainerSpec {
        ContainerSpec {
            docker: vec!["wsl.exe".into(), "-e".into(), "docker".into()],
            image: IMAGE.into(),
            entrypoint: Some("/usr/local/bin/python".into()),
            command: vec!["/input/field/host.py".into()],
            workdir: Some("/work".into()),
            user: None,
            environment: BTreeMap::from([("PLUGIN".to_owned(), "roll".to_owned())]),
            mounts: vec![BindMount {
                source: "/home/u/bundle".into(),
                target: "/input/project".into(),
                read_only: true,
            }],
            tmpfs: vec![Tmpfs {
                target: "/work".into(),
                size_mb: 768,
                exec: true,
            }],
            memory_mb: 1024,
            cpus: 1.5,
            pids_limit: 128,
            network: "none".into(),
        }
    }

    #[test]
    fn generated_command_always_carries_the_hardening_flags() {
        let (program, args) = container_args("roll", "abc123", &spec());
        let args: Vec<String> = args
            .into_iter()
            .map(|arg| arg.into_string().unwrap())
            .collect();
        assert_eq!(program, OsString::from("wsl.exe"));
        assert_eq!(&args[..4], ["-e", "docker", "run", "--rm"]);
        let joined = args.join(" ");
        for expected in [
            "-i",
            "--name ligamina-roll-{spawn}",
            "--label io.ligamina.host=roll",
            "--label io.ligamina.owner=abc123",
            "--network none",
            "--read-only",
            "--cap-drop ALL",
            "--security-opt no-new-privileges",
            "--pids-limit 128",
            "--memory 1024m",
            "--cpus 1.5",
            "--workdir /work",
            "--tmpfs /work:rw,exec,nosuid,nodev,size=768m,mode=1777",
            "--mount type=bind,src=/home/u/bundle,dst=/input/project,readonly",
            "-e PLUGIN=roll",
            "--entrypoint /usr/local/bin/python",
        ] {
            assert!(
                joined.contains(expected),
                "missing `{expected}` in {joined}"
            );
        }
        // Image and command come last, after every flag.
        assert_eq!(&args[args.len() - 2..], [IMAGE, "/input/field/host.py"]);
    }

    #[test]
    fn cleanup_removes_the_same_named_container() {
        let cleanup = cleanup_command("roll", &spec());
        let args: Vec<String> = cleanup
            .args
            .into_iter()
            .map(|arg| arg.into_string().unwrap())
            .collect();
        assert_eq!(cleanup.program, OsString::from("wsl.exe"));
        assert_eq!(args, ["-e", "docker", "rm", "-f", "ligamina-roll-{spawn}"]);
    }

    #[test]
    fn unsafe_specifications_are_rejected_with_the_rule() {
        let cases: Vec<(&str, Box<dyn Fn(&mut ContainerSpec)>)> = vec![
            (
                "pinned by digest",
                Box::new(|s| s.image = "example/runtime:latest".into()),
            ),
            (
                "pinned by digest",
                Box::new(|s| s.image = "example/runtime@sha256:abc".into()),
            ),
            (
                "network must be `none`",
                Box::new(|s| s.network = "bridge".into()),
            ),
            ("must be positive", Box::new(|s| s.memory_mb = 0)),
            ("must be positive", Box::new(|s| s.cpus = 0.0)),
            ("must be positive", Box::new(|s| s.pids_limit = 0)),
            (
                "must be read-only",
                Box::new(|s| s.mounts[0].read_only = false),
            ),
            (
                "may not be `/`",
                Box::new(|s| s.mounts[0].target = "/".into()),
            ),
            (
                "may not be `/proc`",
                Box::new(|s| s.tmpfs[0].target = "/proc/".into()),
            ),
            (
                "absolute container path",
                Box::new(|s| s.mounts[0].target = "input".into()),
            ),
            (
                "without `,`",
                Box::new(|s| s.mounts[0].source = "/a,b".into()),
            ),
            ("command prefix", Box::new(|s| s.docker.clear())),
            (
                "size_mb must be positive",
                Box::new(|s| s.tmpfs[0].size_mb = 0),
            ),
        ];
        for (rule, mutate) in cases {
            let mut candidate = spec();
            mutate(&mut candidate);
            let error = validate("roll", &candidate, false).expect_err(rule).0;
            assert!(error.starts_with("Host roll: "), "{error}");
            assert!(error.contains(rule), "expected `{rule}` in `{error}`");
        }
        assert!(validate("bad id", &spec(), false).is_err());
        assert!(validate("roll", &spec(), false).is_ok());
    }

    #[test]
    fn trusted_hosts_may_use_writable_bind_mounts() {
        let mut candidate = spec();
        candidate.mounts[0].read_only = false;
        assert!(validate("roll", &candidate, true).is_ok());
        let (_, args) = container_args("roll", "o", &candidate);
        let joined: Vec<String> = args
            .into_iter()
            .map(|arg| arg.into_string().unwrap())
            .collect();
        assert!(joined.contains(&"type=bind,src=/home/u/bundle,dst=/input/project".to_owned()));
    }

    #[test]
    fn owner_is_stable_and_short() {
        assert_eq!(owner_for("/a/core.sqlite3"), owner_for("/a/core.sqlite3"));
        assert_ne!(owner_for("/a/core.sqlite3"), owner_for("/b/core.sqlite3"));
        assert_eq!(owner_for("x").len(), 12);
    }
}
