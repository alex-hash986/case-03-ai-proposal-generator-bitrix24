BASE = {
    "deal_id": 301,
    "client_name": "Анна",
    "company_name": "ООО «ПромСнаб»",
    "segment": "standard",
    "items": [
        {"name": "Аудит процесса", "quantity": "1", "unit_price": "45000", "unit_cost": "22000"},
        {"name": "Настройка интеграции", "quantity": "2", "unit_price": "35000", "unit_cost": "17000"},
    ],
    "desired_discount_pct": "5",
    "payment_terms": "По счёту в течение 10 рабочих дней.",
    "implementation_terms": "В течение 15 рабочих дней после согласования.",
}

PRESETS = {
    "Стандартная сделка": BASE,
    "Возражения из звонка": BASE | {
        "deal_id": 302, "client_name": "Илья", "company_name": "АО «РегионСервис»",
        "pains": ["Трудно отследить статус каждой заявки."],
        "agreements": ["Менеджер согласует состав спецификации."],
        "objections": ["Важно сохранить текущий процесс передачи заявок."],
        "mode": "three_tiers",
    },
    "Скидка на согласование РОП": BASE | {
        "deal_id": 303, "client_name": "Мария", "company_name": "ООО «Эксперт»",
        "desired_discount_pct": "35",
    },
}
