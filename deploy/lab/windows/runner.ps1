$ErrorActionPreference = 'Stop'
$spec = Get-Content -LiteralPath 'C:\Ligamina\Control\worker.json' -Raw | ConvertFrom-Json

$jobRunnerSource = @'
using System;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;

public sealed class LabWorkerResult {
    public string status { get; set; }
    public string reason_code { get; set; }
    public int? exit_code { get; set; }
    public double elapsed_seconds { get; set; }
    public string diagnostic { get; set; }
    public bool cleanup_confirmed { get; set; }
    public bool output_truncated { get; set; }
}

public static class LabWorkerJob {
    private const uint CREATE_SUSPENDED = 0x00000004;
    private const uint CREATE_NO_WINDOW = 0x08000000;
    private const uint GENERIC_READ = 0x80000000;
    private const uint GENERIC_WRITE = 0x40000000;
    private const uint FILE_SHARE_READ = 0x00000001;
    private const uint FILE_SHARE_WRITE = 0x00000002;
    private const uint CREATE_ALWAYS = 2;
    private const uint OPEN_EXISTING = 3;
    private const uint FILE_ATTRIBUTE_NORMAL = 0x00000080;
    private const uint STARTF_USESTDHANDLES = 0x00000100;
    private const uint DISABLE_MAX_PRIVILEGE = 0x00000001;
    private const uint LOGON32_LOGON_INTERACTIVE = 2;
    private const uint LOGON32_PROVIDER_DEFAULT = 0;
    private const int TokenIntegrityLevel = 25;
    private const uint SE_GROUP_INTEGRITY = 0x00000020;
    private const uint JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9;
    private const uint JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION_CLASS = 15;
    private const uint JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008;
    private const uint JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200;
    private const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;
    private const uint JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x00000001;
    private const uint JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x00000004;
    private const uint WAIT_OBJECT_0 = 0;
    private const uint WAIT_TIMEOUT = 258;

    [StructLayout(LayoutKind.Sequential)]
    private struct SECURITY_ATTRIBUTES { public int nLength; public IntPtr lpSecurityDescriptor; public bool bInheritHandle; }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct STARTUPINFO {
        public int cb; public string lpReserved; public string lpDesktop; public string lpTitle;
        public int dwX; public int dwY; public int dwXSize; public int dwYSize;
        public int dwXCountChars; public int dwYCountChars; public int dwFillAttribute;
        public uint dwFlags; public short wShowWindow; public short cbReserved2;
        public IntPtr lpReserved2; public IntPtr hStdInput; public IntPtr hStdOutput; public IntPtr hStdError;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct PROCESS_INFORMATION { public IntPtr hProcess; public IntPtr hThread; public uint dwProcessId; public uint dwThreadId; }
    [StructLayout(LayoutKind.Sequential)]
    private struct TOKEN_MANDATORY_LABEL { public SID_AND_ATTRIBUTES Label; }
    [StructLayout(LayoutKind.Sequential)]
    private struct SID_AND_ATTRIBUTES { public IntPtr Sid; public uint Attributes; }
    [StructLayout(LayoutKind.Sequential)]
    private struct BASIC_LIMIT_INFORMATION {
        public long PerProcessUserTimeLimit; public long PerJobUserTimeLimit; public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize; public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit; public UIntPtr Affinity; public uint PriorityClass; public uint SchedulingClass;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct IO_COUNTERS { public ulong ReadOperationCount; public ulong WriteOperationCount; public ulong OtherOperationCount; public ulong ReadTransferCount; public ulong WriteTransferCount; public ulong OtherTransferCount; }
    [StructLayout(LayoutKind.Sequential)]
    private struct EXTENDED_LIMIT_INFORMATION {
        public BASIC_LIMIT_INFORMATION BasicLimitInformation; public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit; public UIntPtr JobMemoryLimit; public UIntPtr PeakProcessMemoryUsed; public UIntPtr PeakJobMemoryUsed;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct CPU_RATE_CONTROL_INFORMATION { public uint ControlFlags; public uint CpuRate; }

    [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)] private static extern bool LogonUser(string user, string domain, string password, uint logonType, uint provider, out IntPtr token);
    [DllImport("advapi32.dll", SetLastError = true)] private static extern bool CreateRestrictedToken(IntPtr existing, uint flags, uint disableCount, IntPtr disableSids, uint deletePrivilegeCount, IntPtr deletePrivileges, uint restrictCount, IntPtr restrictSids, out IntPtr token);
    [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)] private static extern bool ConvertStringSidToSid(string stringSid, out IntPtr sid);
    [DllImport("advapi32.dll", SetLastError = true)] private static extern uint GetLengthSid(IntPtr sid);
    [DllImport("advapi32.dll", SetLastError = true)] private static extern bool SetTokenInformation(IntPtr token, int infoClass, IntPtr info, uint length);
    [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)] private static extern bool CreateProcessAsUser(IntPtr token, string app, StringBuilder commandLine, IntPtr processAttributes, IntPtr threadAttributes, bool inheritHandles, uint flags, IntPtr environment, string currentDirectory, ref STARTUPINFO startupInfo, out PROCESS_INFORMATION processInfo);
    [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)] private static extern bool CreateProcessWithToken(IntPtr token, uint logonFlags, string app, StringBuilder commandLine, uint flags, IntPtr environment, string currentDirectory, ref STARTUPINFO startupInfo, out PROCESS_INFORMATION processInfo);
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)] private static extern IntPtr CreateFile(string name, uint access, uint share, ref SECURITY_ATTRIBUTES security, uint creation, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)] private static extern IntPtr CreateJobObject(IntPtr attributes, string name);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool SetInformationJobObject(IntPtr job, uint infoClass, IntPtr info, uint length);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool GetExitCodeProcess(IntPtr process, out uint exitCode);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool TerminateJobObject(IntPtr job, uint exitCode);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool FlushFileBuffers(IntPtr handle);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool LocalFree(IntPtr memory);

    public static LabWorkerResult Run(string user, string password, string[] command, string workingDirectory, string stdoutPath, string stderrPath, int timeoutSeconds, int memoryMb, int processLimit, double cpuCores, int outputLimitBytes) {
        var watch = Stopwatch.StartNew();
        IntPtr job = IntPtr.Zero, token = IntPtr.Zero, restricted = IntPtr.Zero, lowSid = IntPtr.Zero, adminSid = IntPtr.Zero, localAdminSid = IntPtr.Zero, disabledBuffer = IntPtr.Zero, labelBuffer = IntPtr.Zero;
        IntPtr stdout = IntPtr.Zero, stderr = IntPtr.Zero, stdin = IntPtr.Zero;
        PROCESS_INFORMATION process = new PROCESS_INFORMATION();
        string status = "unknown", reason = "worker_guest_restriction_failed";
        string diagnostic = null;
        bool outputTruncated = false, cleanupConfirmed = false;
        string stage = "create_job";
        int? exitCode = null;
        try {
            job = CreateJobObject(IntPtr.Zero, null);
            if (job == IntPtr.Zero) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            stage = "set_job_limits";
            var extended = new EXTENDED_LIMIT_INFORMATION();
            extended.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_ACTIVE_PROCESS | JOB_OBJECT_LIMIT_JOB_MEMORY | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            extended.BasicLimitInformation.ActiveProcessLimit = (uint)processLimit;
            extended.JobMemoryLimit = new UIntPtr((ulong)memoryMb * 1024UL * 1024UL);
            SetInfo(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, extended);
            var cpu = new CPU_RATE_CONTROL_INFORMATION();
            cpu.ControlFlags = JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP;
            cpu.CpuRate = (uint)Math.Max(1, Math.Min(10000, 10000.0 * cpuCores / Math.Max(1, Environment.ProcessorCount)));
            SetInfo(job, JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION_CLASS, cpu);

            stage = "logon_worker_user";
            if (!LogonUser(user, ".", password, LOGON32_LOGON_INTERACTIVE, LOGON32_PROVIDER_DEFAULT, out token)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            stage = "create_restricted_token";
            if (!CreateRestrictedToken(token, DISABLE_MAX_PRIVILEGE, 0, IntPtr.Zero, 0, IntPtr.Zero, 0, IntPtr.Zero, out restricted)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            stage = "set_low_integrity";
            if (!ConvertStringSidToSid("S-1-16-4096", out lowSid)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            labelBuffer = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(TOKEN_MANDATORY_LABEL)) + (int)GetLengthSid(lowSid));
            var label = new TOKEN_MANDATORY_LABEL { Label = new SID_AND_ATTRIBUTES { Sid = lowSid, Attributes = SE_GROUP_INTEGRITY } };
            Marshal.StructureToPtr(label, labelBuffer, false);
            if (!SetTokenInformation(restricted, TokenIntegrityLevel, labelBuffer, (uint)(Marshal.SizeOf(typeof(TOKEN_MANDATORY_LABEL)) + (int)GetLengthSid(lowSid)))) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());

            stage = "open_output_handles";
            var security = new SECURITY_ATTRIBUTES { nLength = Marshal.SizeOf(typeof(SECURITY_ATTRIBUTES)), bInheritHandle = true };
            stdout = CreateFile(stdoutPath, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, ref security, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, IntPtr.Zero);
            stderr = CreateFile(stderrPath, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, ref security, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, IntPtr.Zero);
            stdin = CreateFile("NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, ref security, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, IntPtr.Zero);
            if (IsInvalid(stdout) || IsInvalid(stderr) || IsInvalid(stdin)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());

            var startup = new STARTUPINFO();
            startup.cb = Marshal.SizeOf(typeof(STARTUPINFO)); startup.dwFlags = STARTF_USESTDHANDLES;
            startup.hStdInput = stdin; startup.hStdOutput = stdout; startup.hStdError = stderr;
            var commandLine = new StringBuilder(String.Join(" ", Array.ConvertAll(command, QuoteArgument)));
            stage = "create_restricted_process";
            bool created = CreateProcessAsUser(restricted, command[0], commandLine, IntPtr.Zero, IntPtr.Zero, true, CREATE_SUSPENDED | CREATE_NO_WINDOW, IntPtr.Zero, workingDirectory, ref startup, out process);
            if (!created) created = CreateProcessWithToken(restricted, 0, command[0], commandLine, CREATE_SUSPENDED | CREATE_NO_WINDOW, IntPtr.Zero, workingDirectory, ref startup, out process);
            if (!created) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            stage = "assign_job";
            if (!AssignProcessToJobObject(job, process.hProcess)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            stage = "resume_process";
            if (ResumeThread(process.hThread) == 0xffffffff) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());

            stage = "wait_for_process";
            var deadline = watch.Elapsed + TimeSpan.FromSeconds(Math.Max(1, timeoutSeconds));
            while (true) {
                uint wait = WaitForSingleObject(process.hProcess, 100);
                if (wait == WAIT_OBJECT_0) {
                    uint code;
                    if (!GetExitCodeProcess(process.hProcess, out code)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
                    exitCode = (int)code;
                    status = code == 0 ? "passed" : "failed";
                    reason = code == 0 ? null : "worker_nonzero_exit";
                    break;
                }
                if (wait != WAIT_TIMEOUT) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
                if (new System.IO.FileInfo(stdoutPath).Length > outputLimitBytes || new System.IO.FileInfo(stderrPath).Length > outputLimitBytes) {
                    outputTruncated = true; status = "unknown"; reason = "worker_output_truncated";
                    TerminateJobObject(job, 125);
                    break;
                }
                if (watch.Elapsed >= deadline) {
                    status = "unknown"; reason = "worker_timeout";
                    TerminateJobObject(job, 124);
                    break;
                }
            }
            stage = "worker_tree_cleanup";
            uint jobWait = WaitForSingleObject(job, 250);
            if (jobWait == WAIT_TIMEOUT) {
                TerminateJobObject(job, 126);
                jobWait = WaitForSingleObject(job, 5000);
            }
            cleanupConfirmed = jobWait == WAIT_OBJECT_0;
            if (!cleanupConfirmed) { status = "unknown"; reason = "worker_cleanup_unconfirmed"; }
        } catch (Exception error) {
            status = "unknown"; reason = "worker_guest_restriction_failed";
            var win32 = error as System.ComponentModel.Win32Exception;
            diagnostic = stage + ":" + error.GetType().Name + ":0x" + error.HResult.ToString("X8") + ":" + (win32 == null ? "" : win32.NativeErrorCode + ":") + error.Message;
            if (diagnostic.Length > 512) diagnostic = diagnostic.Substring(0, 512);
        } finally {
            if (job != IntPtr.Zero) {
                if (!cleanupConfirmed) {
                    TerminateJobObject(job, 127);
                    cleanupConfirmed = WaitForSingleObject(job, 5000) == WAIT_OBJECT_0;
                }
                CloseHandle(job);
            }
            if (process.hThread != IntPtr.Zero) CloseHandle(process.hThread);
            if (process.hProcess != IntPtr.Zero) CloseHandle(process.hProcess);
            if (stdout != IntPtr.Zero && !IsInvalid(stdout)) { FlushFileBuffers(stdout); CloseHandle(stdout); }
            if (stderr != IntPtr.Zero && !IsInvalid(stderr)) { FlushFileBuffers(stderr); CloseHandle(stderr); }
            if (stdin != IntPtr.Zero && !IsInvalid(stdin)) CloseHandle(stdin);
            if (labelBuffer != IntPtr.Zero) Marshal.FreeHGlobal(labelBuffer);
            if (lowSid != IntPtr.Zero) LocalFree(lowSid);
            if (restricted != IntPtr.Zero) CloseHandle(restricted);
            if (token != IntPtr.Zero) CloseHandle(token);
        }
        watch.Stop();
        return new LabWorkerResult { status = status, reason_code = reason, exit_code = exitCode, elapsed_seconds = watch.Elapsed.TotalSeconds, diagnostic = diagnostic, cleanup_confirmed = cleanupConfirmed, output_truncated = outputTruncated };
    }

    private static void SetInfo<T>(IntPtr job, uint infoClass, T value) where T : struct {
        int size = Marshal.SizeOf(typeof(T)); IntPtr buffer = Marshal.AllocHGlobal(size);
        try { Marshal.StructureToPtr(value, buffer, false); if (!SetInformationJobObject(job, infoClass, buffer, (uint)size)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error()); }
        finally { Marshal.FreeHGlobal(buffer); }
    }
    private static bool IsInvalid(IntPtr value) { return value == IntPtr.Zero || value == new IntPtr(-1); }
    private static string QuoteArgument(string value) {
        if (value.Length > 0 && value.IndexOfAny(new char[] {' ', '\t', '"'}) < 0) return value;
        var output = new StringBuilder(); output.Append('"'); int slashes = 0;
        foreach (char c in value) {
            if (c == '\\') { slashes++; continue; }
            if (c == '"') { output.Append('\\', slashes * 2 + 1); output.Append('"'); slashes = 0; continue; }
            output.Append('\\', slashes); slashes = 0; output.Append(c);
        }
        output.Append('\\', slashes * 2); output.Append('"'); return output.ToString();
    }
}
'@

$runnerStage = 'workspace'
try {
  $trustedRoot = Join-Path $env:ProgramData 'Ligamina'
  New-Item -ItemType Directory -Path $trustedRoot, (Split-Path $spec.result_path), $spec.outbound_path -Force | Out-Null
  $runnerStage = 'protect_internal_output'
  & icacls.exe $trustedRoot /inheritance:r /grant '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' /T | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'Could not protect private runner output.' }
  $runnerStage = 'workspace'
  New-Item -ItemType Directory -Path $spec.working_directory -Force | Out-Null
  $workspaceTemp = Join-Path $spec.working_directory 'Temp'
  $workspaceHome = Join-Path $spec.working_directory 'Home'
  New-Item -ItemType Directory -Path $workspaceTemp, $workspaceHome -Force | Out-Null
  $guestInput = [string]$spec.guest_input_path
  New-Item -ItemType Directory -Path $guestInput -Force | Out-Null
  $runnerStage = 'stage_input'
  Get-ChildItem -LiteralPath $spec.input_path -Force | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $guestInput $_.Name) -Recurse -Force
  }
  $workerUser = 'Lig' + [Guid]::NewGuid().ToString('N').Substring(0, 12)
  $workerPassword = [Guid]::NewGuid().ToString('N') + 'Aa1!'
  $runnerStage = 'create_worker_account'
  $securePassword = ConvertTo-SecureString $workerPassword -AsPlainText -Force
  $workerAccount = New-LocalUser -Name $workerUser -Password $securePassword -PasswordNeverExpires -AccountNeverExpires -ErrorAction Stop
  $workerSid = $workerAccount.SID.Value
  $runnerStage = 'grant_worker_workspace'
  $workspaceAcl = '*{0}:(OI)(CI)M' -f $workerSid
  & icacls.exe $spec.working_directory /grant $workspaceAcl /T | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'Could not grant workspace access to the disposable worker account.' }
  $inputAcl = '*{0}:(OI)(CI)RX' -f $workerSid
  & icacls.exe $guestInput /grant $inputAcl /T | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'Could not grant read-only input access to the disposable worker account.' }
  $runnerStage = 'integrity_label'
  & icacls.exe $spec.working_directory /setintegritylevel Low | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'Could not apply low-integrity workspace label.' }
  $env:TEMP = $workspaceTemp
  $env:TMP = $workspaceTemp
  $env:USERPROFILE = $workspaceHome
  $env:APPDATA = Join-Path $workspaceHome 'AppData\Roaming'
  $env:LOCALAPPDATA = Join-Path $workspaceHome 'AppData\Local'
  $runnerStage = 'compile_job_runner'
  Add-Type -TypeDefinition $jobRunnerSource -ErrorAction Stop
  $runnerStage = 'launch_restricted_process'
  $result = [LabWorkerJob]::Run(
    [string]$workerUser,
    [string]$workerPassword,
    [string[]]$spec.command,
    [string]$spec.working_directory,
    [string]$spec.stdout_path,
    [string]$spec.stderr_path,
    [int]$spec.timeout_seconds,
    [int]$spec.memory_mb,
    [int]$spec.process_limit,
    [double]$spec.cpu_cores,
    [int]$spec.output_limit_bytes
  )
} catch {
  $message = ($_.Exception.Message -replace '[\r\n]+', ' ')
  if ($message.Length -gt 512) { $message = $message.Substring(0, 512) }
  $result = [pscustomobject]@{
    status = 'unknown'; reason_code = 'sandbox_runner_failed'; exit_code = $null
    elapsed_seconds = 0; cleanup_confirmed = $true; output_truncated = $false
    diagnostic = ('{0}:0x{1:X8}:{2}' -f $runnerStage, [int]$_.Exception.HResult, $message)
  }
}
$temporary = [string]$spec.result_path + '.tmp'
[IO.File]::WriteAllText($temporary, ($result | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $temporary -Destination $spec.result_path -Force
