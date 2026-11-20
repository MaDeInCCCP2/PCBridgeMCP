"""One bounded capture per subprocess. DXGI uses an optional local package."""
from pathlib import Path
import sys


def main():
    method, hwnd, client, out = sys.argv[1:]
    output = Path(out)
    if method == "window":
        import server
        frame = server._print_window(int(hwnd), client == "1")
        if frame is None or server._blank(frame):
            raise RuntimeError("PrintWindow returned a blank or missing image")
        frame.save(output)
    else:
        from windows_capture import DxgiDuplicationSession
        session = DxgiDuplicationSession()
        frame = session.acquire_frame(timeout_ms=800)
        if frame is None:
            raise RuntimeError("No fresh DXGI frame arrived")
        frame.save_as_image(str(output))


if __name__ == "__main__":
    main()
