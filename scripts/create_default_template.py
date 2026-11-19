from src.core.config import load_settings
from src.domain.doc_renderer import create_default_template

if __name__ == "__main__":
    settings = load_settings()
    create_default_template(settings.template, settings.brand)
    print(settings.template)
