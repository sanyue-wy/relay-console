"""启动控制台：python run_console.py"""
import uvicorn

from relay.config import load_config

if __name__ == "__main__":
    cfg = load_config()
    uvicorn.run("relay.app:app", host=cfg["host"], port=int(cfg["port"]), log_level="info")
