import re
import unicodedata
from typing import Literal

from pydantic import ValidationError

from src.core.config import ROOT
from src.core.llm_gateway import CycleUsage, InvalidCompletion, LLMGateway, known_fields
from src.domain.schemas import DealContextInput, LLMProposalCopy


class NumericPromise(InvalidCompletion):
    pass


NUMERIC = re.compile(
    r"\d|[₽€$£%‰]|\b(?:нул\w*|один|одна|одну|одного|одной|два|две|двух|двум\w*|дво[еи]\w*|"
    r"три|тр[её]х|тр[её]м|тремя|четыр\w*|пять|пяти|пятер\w*|пяты\w*|шест\w*|семь|семи|семер\w*|седьм\w*|восем\w*|девят\w*|"
    r"десят\w*|\w*надцат\w*|\w*дцат\w*|сто|сот|сот[еня]|сотн\w*|соты\w*|"
    r"двест[иея]|трист[ао]|четырест[ао]|\w+сот|тысяч\w*|миллион\w*|миллиард\w*|"
    r"перв\w*|втор\w*|треть\w*|половин\w*|полтор\w*|четверт\w*|двойн\w*|тройн\w*|"
    r"сут\w*|час\w*|дн[яеи]\w*|день|дней|недел\w*|"
    r"месяц\w*|год\w*|квартал\w*|рубл\w*|копе\w*|процент\w*|бесплат\w*|"
    r"zero|one|two|three|four|five|six|seven|eight|nine|ten|hundred|thousand|"
    r"first|second|days?|weeks?|months?|years?|free)\b|"
    r"\b[IVXLCDM]+\b",
    re.IGNORECASE,
)
UNSAFE_TERMS = re.compile(
    r"\b(?:скид\w*|цен[аеуы]\w*|срок\w*|оплат\w*|рассроч\w*|предоплат\w*|"
    r"гарантир\w*|завтра|сегодня|немедленно|сразу|дисконт\w*|даром|"
    r"безвозмезд\w*|уцен\w*|кешб[эе]к\w*|cashback|rebate|discount)\b|"
    r"\b(?:в подарок|за наш сч[её]т|без доплат\w*|без взимания платы|"
    r"льготный период|снизим стоимость|снижение стоимости|по себестоимости)\b",
    re.IGNORECASE,
)


def verify_no_numeric_hallucinations(copy: LLMProposalCopy) -> None:
    for field, value in copy.model_dump().items():
        normalized = "".join(
            char for char in unicodedata.normalize("NFKC", value)
            if unicodedata.category(char) != "Cf"
        )
        if (
            (field in {"payment_terms", "implementation_terms"} and value)
            or any(character.isnumeric() for character in normalized)
            or NUMERIC.search(normalized)
            or UNSAFE_TERMS.search(normalized)
        ):
            raise NumericPromise(f"Неподтверждённые коммерческие данные в поле {field}")


def mock_copy(context: DealContextInput) -> LLMProposalCopy:
    return LLMProposalCopy(
        greeting="Здравствуйте!",
        value_proposition=(
            f"В обсуждении вы отметили: {context.pains[0]}"
            if context.pains else "Подготовили предложение по вашей спецификации."
        ),
        objection_handling=(
            f"Готовы обсудить ваш вопрос: {context.objections[0]}"
            if context.objections else "Готовы ответить на вопросы по составу предложения."
        ),
        payment_terms="", implementation_terms="",
        cover_letter="Направляем предложение. Будем рады обсудить детали.",
    )


async def write_copy(
    context: DealContextInput, gateway: LLMGateway, usage: CycleUsage,
) -> tuple[
    LLMProposalCopy, Literal["mock", "live", "fallback"],
    Literal["invalid_copy", "numeric_guard"] | None,
]:
    mode: Literal["mock", "live", "fallback"] = gateway.settings.llm_mode
    reason: Literal["invalid_copy", "numeric_guard"] | None = None
    try:
        if mode == "mock":
            copy = mock_copy(context)
        else:
            system = gateway.load_prompt(
                ROOT / "config/prompts/proposal.yaml", gateway.settings.ab_variant,
            )
            payload = context.model_dump_json(include={
                "client_name", "company_name", "segment", "pains", "objections", "agreements",
            })
            data = await gateway.evaluate(system, payload, usage)
            copy = LLMProposalCopy.model_validate(known_fields(data, LLMProposalCopy))
        verify_no_numeric_hallucinations(copy)
    except (InvalidCompletion, ValidationError) as exc:
        reason = "numeric_guard" if isinstance(exc, NumericPromise) else "invalid_copy"
        copy = mock_copy(context.model_copy(update={"pains": [], "objections": []}))
        mode = "fallback"
    return copy.model_copy(update={
        "greeting": f"{context.client_name}, {copy.greeting[0].lower() + copy.greeting[1:]}"
        if copy.greeting else f"{context.client_name}, здравствуйте!",
        "payment_terms": context.payment_terms or "Требуется согласование.",
        "implementation_terms": context.implementation_terms or "Требуется согласование.",
    }), mode, reason
