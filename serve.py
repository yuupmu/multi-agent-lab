"""웹 UI + OpenAI / 모의 에이전트 백엔드."""
import argparse
from pathlib import Path
from backend.server import create_server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    server, engine = create_server(Path(__file__).resolve().parent, args.port, args.data_dir)
    try:
        print(f"Local: http://127.0.0.1:{args.port}", flush=True)
        print("사이트에서 API 키 연결 또는 모의 실행 · 종료: Ctrl+C", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    finally:
        server.server_close()
        engine.close()


if __name__ == "__main__":
    main()
