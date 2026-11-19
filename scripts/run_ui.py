from streamlit.web import cli

from src.core.config import ROOT, load_settings

if __name__ == "__main__":
    settings = load_settings()
    cli.main(
        args=["run", str(ROOT / "src/ui/app.py"), "--server.address", "127.0.0.1",
              "--server.port", str(settings.ui_port)],
        standalone_mode=True,
    )
