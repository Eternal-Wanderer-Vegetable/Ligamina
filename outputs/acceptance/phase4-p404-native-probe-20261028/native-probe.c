#define WIN32_LEAN_AND_MEAN
#include <windows.h>

int wmain(void) {
    static const wchar_t marker_path[] = L"C:\\P404Results\\native-probe-marker.txt";
    static const char marker[] = "P4-04 self-owned native probe reached main\r\n";
    DWORD written = 0;
    HANDLE file = CreateFileW(marker_path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (file == INVALID_HANDLE_VALUE) return 20;
    if (!WriteFile(file, marker, (DWORD)(sizeof(marker) - 1), &written, NULL)) {
        CloseHandle(file);
        return 21;
    }
    if (written != (DWORD)(sizeof(marker) - 1)) {
        CloseHandle(file);
        return 22;
    }
    if (!FlushFileBuffers(file)) {
        CloseHandle(file);
        return 23;
    }
    if (!CloseHandle(file)) return 24;
    return 0;
}
