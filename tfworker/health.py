"""배포된 앱이 응답할 때까지 기다린다."""
import time
import urllib.error
import urllib.request


def wait_healthy(url: str, timeout: int = 420, interval: int = 10) -> bool:
    start = time.time()
    print(f"[health] waiting for {url} (최대 {timeout}초)", flush=True)
    while True:
        elapsed = int(time.time() - start)
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                if 200 <= r.status < 400:
                    print(f"[health] OK ({r.status}) after {elapsed}s", flush=True)
                    return True
                status = r.status
        except urllib.error.HTTPError as e:
            status = e.code
        except Exception as e:  # 연결 거부, 타임아웃 등
            status = type(e).__name__
        if elapsed >= timeout:
            print(f"[health] TIMEOUT after {elapsed}s (last: {status})", flush=True)
            return False
        print(f"[health] {elapsed}s: {status}, {interval}초 후 재시도", flush=True)
        time.sleep(interval)
