import json
from pathlib import Path
from uuid import uuid4

import httpx
import streamlit as st

from src.core.config import load_settings
from src.domain.schemas import ProposalGenerationResponse
from src.ui.presets import PRESETS

settings = load_settings()
api = f"http://127.0.0.1:{settings.api_port}"

st.set_page_config(page_title="Генератор КП", layout="wide")
st.title("Коммерческие предложения")
demo, dashboard = st.tabs(["Live Demo", "Executive ROI-дашборд"])

with demo:
    preset = st.selectbox("Сценарий", list(PRESETS))
    payload = st.text_area(
        "Контекст сделки (JSON)", json.dumps(PRESETS[preset], ensure_ascii=False, indent=2),
        key=f"context_{preset}", height=350,
    )
    simulate = st.checkbox("Имитировать потерю ответа Bitrix24")
    if st.button("Сформировать КП", type="primary"):
        try:
            body = json.loads(payload)
            key = st.session_state.setdefault(f"key:{preset}:{payload}", uuid4().hex)
            st.session_state.pop("proposal", None)
            with st.spinner("Формируем предложение"):
                response = httpx.post(
                    f"{api}/api/v1/proposals/generate", json=body,
                    params={"simulate_failure": simulate}, headers={"Idempotency-Key": key},
                    timeout=60,
                )
                response.raise_for_status()
            st.session_state["proposal"] = (preset, payload, response.json())
        except (ValueError, httpx.HTTPError) as exc:
            st.error(str(exc))

    current = st.session_state.get("proposal")
    if current and current[:2] == (preset, payload):
        result = ProposalGenerationResponse.model_validate(current[2])
        if result.status == "reconcile":
            st.warning("Bitrix24 не подтвердил публикацию. Требуется сверка.")
            if st.button("Сверить публикацию"):
                try:
                    response = httpx.post(
                        f"{api}/api/v1/proposals/reconcile",
                        json={"idempotency_key": result.idempotency_key}, timeout=60,
                    )
                    response.raise_for_status()
                    st.session_state["proposal"] = (preset, payload, response.json())
                    st.rerun()
                except httpx.HTTPError as exc:
                    st.error(str(exc))
        elif result.need_human:
            st.warning("КП передано на внутреннюю проверку.")
            for reason in result.margin_audit.rejection_reasons:
                st.write(reason)
        else:
            st.success("КП опубликовано в мок-карточке сделки.")
        for variant in result.financials:
            st.metric(variant.label, f"{variant.total:,.2f} ₽")
        for label, name, mime in [
            ("DOCX", result.docx_path,
             "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            ("PDF", result.pdf_path, "application/pdf"),
        ]:
            artifact = Path(name).resolve()
            if artifact.is_relative_to(settings.artifacts.resolve()) and artifact.is_file():
                st.download_button(f"Скачать {label}", artifact.read_bytes(), artifact.name, mime)
        st.text_area("Сопроводительное письмо", result.proposal_copy.cover_letter, disabled=True)

with dashboard:
    try:
        response = httpx.get(f"{api}/api/v1/metrics", timeout=5)
        response.raise_for_status()
        rows = response.json()["by_mode"]
        if rows:
            st.dataframe(rows, hide_index=True)
            st.bar_chart(rows, x="mode", y="avg_cycle_seconds")
        else:
            st.info("Проведите первый цикл, чтобы увидеть метрики.")
        if not any(row["saved_minutes"] is not None for row in rows):
            st.caption("Экономия времени появится после ввода измеренного ручного baseline.")
    except httpx.HTTPError:
        st.error(f"API недоступен по адресу {api}.")
