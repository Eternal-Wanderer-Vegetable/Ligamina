use std::{
    env,
    io::{Read, Write},
    net::{TcpStream, ToSocketAddrs},
    time::Duration,
};

use anyhow::{Context, bail};
use ligamina_cli::mcp::LigaminaMcp;
use ligamina_core::broker::{BrokerError, CapabilityBroker};
use rmcp::{ServiceExt, transport::io::stdio};
use serde_json::{Value, json};

const HELP: &str = "Ligamina local runtime\n\
Usage:\n\
  ligamina-cli daemon --config <path.json>\n\
  ligamina-cli status --url http://127.0.0.1:<port>\n\
  ligamina-cli doctor --url http://127.0.0.1:<port>\n\
  ligamina-cli report <report.json> [--format json|text|ndjson] [--gate completion|automation|compatibility]\n\
Set LIGAMINA_ADMIN_TOKEN for status and doctor. Only loopback HTTP URLs are accepted.\n";

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let mut arguments = env::args().skip(1);
    let Some(command) = arguments.next() else {
        let broker = CapabilityBroker::new(
            |host_id, _epoch, _capability_id, _arguments, _context| async move {
                Err(BrokerError::HostUnavailable(host_id))
            },
        );
        let service = LigaminaMcp::new(broker).serve(stdio()).await?;
        service.waiting().await?;
        return Ok(());
    };

    match command.as_str() {
        "daemon" => {
            let option = arguments.next();
            let path = arguments.next();
            if option.as_deref() != Some("--config") || path.is_none() {
                bail!("usage: ligamina-cli daemon --config <path.json>");
            }
            if arguments.next().is_some() {
                bail!("unexpected daemon arguments");
            }
            ligamina_cli::daemon::run(path.unwrap().into()).await
        }
        "status" | "doctor" => {
            let url = parse_url_option(arguments)?;
            let token = env::var("LIGAMINA_ADMIN_TOKEN")
                .context("set LIGAMINA_ADMIN_TOKEN to the daemon admin bearer token")?;
            let status = fetch_status(&url, &token)?;
            if command == "status" {
                println!("{}", serde_json::to_string_pretty(&status)?);
                return Ok(());
            }
            let mut checks = Vec::new();
            let schema_ok = status
                .get("schema_version")
                .and_then(Value::as_u64)
                .is_some_and(|version| version > 0);
            checks.push(json!({
                "id": "core_schema",
                "status": if schema_ok { "passed" } else { "failed" },
                "observed": status.get("schema_version"),
            }));
            let hosts = status
                .get("hosts")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            let host_checks = hosts
                .iter()
                .map(|host| {
                    let active = host.get("state").and_then(Value::as_str) == Some("active")
                        && host
                            .get("pid")
                            .and_then(Value::as_u64)
                            .is_some_and(|pid| pid > 0);
                    json!({
                        "id": host.get("id"),
                        "status": if active { "passed" } else { "failed" },
                        "state": host.get("state"),
                        "pid": host.get("pid"),
                        "restart_count": host.get("restart_count"),
                        "last_error": host.get("last_error"),
                    })
                })
                .collect::<Vec<_>>();
            let hosts_ok = !hosts.is_empty()
                && host_checks
                    .iter()
                    .all(|host| host.get("status").and_then(Value::as_str) == Some("passed"));
            checks.push(json!({
                "id": "managed_hosts",
                "status": if hosts_ok { "passed" } else { "failed" },
                "expected_denominator": hosts.len(),
                "observed": host_checks,
            }));
            let passed = schema_ok && hosts_ok;
            println!(
                "{}",
                serde_json::to_string_pretty(&json!({
                    "suite": "doctor",
                    "status": if passed { "passed" } else { "failed" },
                    "core_epoch": status.get("core_epoch"),
                    "checks": checks,
                }))?
            );
            if !passed {
                std::process::exit(1);
            }
            Ok(())
        }
        "report" => {
            let Some(path) = arguments.next() else {
                eprintln!("invalid report arguments");
                std::process::exit(2);
            };
            let mut output_format = "json".to_owned();
            let mut format_seen = false;
            let mut selected_gate = None;
            while let Some(option) = arguments.next() {
                match option.as_str() {
                    "--format" if !format_seen => {
                        let Some(value) = arguments.next() else {
                            eprintln!("invalid report arguments");
                            std::process::exit(2);
                        };
                        output_format = value;
                        format_seen = true;
                    }
                    "--gate" if selected_gate.is_none() => {
                        let Some(value) = arguments.next() else {
                            eprintln!("invalid report arguments");
                            std::process::exit(2);
                        };
                        selected_gate = Some(value);
                    }
                    _ => {
                        eprintln!("invalid report arguments");
                        std::process::exit(2);
                    }
                }
            }
            let contents = match std::fs::read(path) {
                Ok(contents) => contents,
                Err(_) => {
                    eprintln!("could not read report JSON");
                    std::process::exit(2);
                }
            };
            let report: Value = match serde_json::from_slice(&contents) {
                Ok(report) => report,
                Err(_) => {
                    eprintln!("could not parse report JSON");
                    std::process::exit(2);
                }
            };
            let (rendered, code) = match ligamina_cli::report::render_report(
                &report,
                &output_format,
                selected_gate.as_deref(),
            ) {
                Ok(rendered) => rendered,
                Err(_) => {
                    eprintln!("report failed schema, denominator, or gate validation");
                    std::process::exit(2);
                }
            };
            println!("{rendered}");
            if code != 0 {
                std::process::exit(code);
            }
            Ok(())
        }
        "help" | "--help" | "-h" => {
            print!("{HELP}");
            Ok(())
        }
        _ => bail!("unknown command; run `ligamina-cli --help` for available commands"),
    }
}

fn parse_url_option(mut arguments: impl Iterator<Item = String>) -> anyhow::Result<String> {
    let mut url = None;
    while let Some(argument) = arguments.next() {
        if argument != "--url" || url.is_some() {
            bail!("usage: ligamina-cli status|doctor --url http://127.0.0.1:<port>");
        }
        url = Some(arguments.next().context("--url requires a value")?);
    }
    url.context("usage: ligamina-cli status|doctor --url http://127.0.0.1:<port>")
}

fn fetch_status(url: &str, token: &str) -> anyhow::Result<Value> {
    let address = url
        .strip_prefix("http://")
        .context("only loopback http:// URLs are supported")?;
    let authority = address.split('/').next().unwrap_or_default();
    let (host, port) = authority
        .rsplit_once(':')
        .context("URL must include an explicit port")?;
    if !matches!(host, "127.0.0.1" | "localhost" | "[::1]") {
        bail!("refusing to send the admin token to a non-loopback host");
    }
    let port: u16 = port.parse().context("invalid URL port")?;
    let socket = format!("{host}:{port}")
        .to_socket_addrs()
        .context("could not resolve loopback endpoint")?
        .next()
        .context("loopback endpoint has no address")?;
    let mut stream = TcpStream::connect_timeout(&socket, Duration::from_secs(3))
        .context("could not connect to Core daemon")?;
    stream.set_read_timeout(Some(Duration::from_secs(5)))?;
    stream.set_write_timeout(Some(Duration::from_secs(5)))?;
    stream.write_all(
        format!(
            "GET /v1/status HTTP/1.1\r\nHost: {host}:{port}\r\nAuthorization: Bearer {token}\r\nConnection: close\r\nAccept: application/json\r\n\r\n"
        )
        .as_bytes(),
    )?;
    let mut response = Vec::new();
    stream
        .read_to_end(&mut response)
        .context("could not read Core status response")?;
    let split = response
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .context("invalid HTTP response from Core")?;
    let headers = String::from_utf8_lossy(&response[..split]);
    let status = headers
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .context("HTTP status line was missing")?
        .parse::<u16>()
        .context("invalid HTTP status code")?;
    let body = &response[split + 4..];
    if status != 200 {
        bail!(
            "Core returned HTTP {status}: {}",
            String::from_utf8_lossy(body)
        );
    }
    let body = if headers
        .to_ascii_lowercase()
        .contains("transfer-encoding: chunked")
    {
        decode_chunked(body)?
    } else {
        body.to_vec()
    };
    serde_json::from_slice(&body).context("Core returned invalid status JSON")
}

fn decode_chunked(body: &[u8]) -> anyhow::Result<Vec<u8>> {
    let mut decoded = Vec::new();
    let mut offset = 0;
    loop {
        let line_end = body[offset..]
            .windows(2)
            .position(|window| window == b"\r\n")
            .context("invalid chunked HTTP response")?
            + offset;
        let size = usize::from_str_radix(
            String::from_utf8_lossy(&body[offset..line_end])
                .split(';')
                .next()
                .unwrap_or_default()
                .trim(),
            16,
        )
        .context("invalid HTTP chunk size")?;
        offset = line_end + 2;
        if size == 0 {
            return Ok(decoded);
        }
        let end = offset
            .checked_add(size)
            .context("HTTP chunk size overflow")?;
        if body.get(end..end + 2) != Some(b"\r\n") {
            bail!("truncated HTTP chunk");
        }
        decoded.extend_from_slice(&body[offset..end]);
        offset = end + 2;
    }
}
