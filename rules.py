import re
from pathlib import Path
from typing import Any
from copy import deepcopy

def _not_empty(value: Any) -> bool:
    return value is not None and str(value).strip() != ""

def _condition_active(condition: str, response: dict[str, Any]) -> bool:
    if not condition:
        return False
    match = re.fullmatch(r"([^=]+)=(.+)", condition.strip())
    if not match:
        return False
    field_id, expected = match.groups()
    return str(response.get(field_id, "")).strip().casefold() == expected.strip().casefold()

def validate_response(response: dict[str, Any], schema: dict[str, dict[str, Any]], workbook_dir: Path) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    warnings: list[dict[str, str]] = []
    attachments: list[dict[str, Any]] = []

    def warn(code: str, message: str, fields: str) -> None:
        warnings.append({"code": code, "message": message, "field_ids": fields})

    for field_id, rule in schema.items():
        required = str(rule.get("obrigatório") or "").strip()
        condition = str(rule.get("condição_exibição") or "").strip()
        value = response.get(field_id)
        active_required = required == "Sim" or (
            required == "Condicional" and _condition_active(condition, response)
        )
        if active_required and not _not_empty(value):
            warn("campo_obrigatorio_ausente", f"Campo obrigatório ausente: {field_id}.", field_id)

        if str(rule.get("tipo") or "") == "arquivo" and _not_empty(value):
            relative = Path(str(value).replace("\\", "/"))
            resolved = (workbook_dir / relative).resolve()
            exists = resolved.exists()
            attachments.append(
                {
                    "field_id": field_id,
                    "declared_path": str(value),
                    "resolved_path": str(resolved),
                    "exists": exists,
                    "status": "Recebido" if exists else "Não localizado",
                }
            )
            if not exists:
                warn("anexo_nao_localizado", f"Anexo não localizado: {value}.", field_id)

    location_other_fields = (
        ("country", "country_other", "país"),
        ("state_region", "state_region_other", "estado, província ou região"),
        ("city", "city_other", "cidade"),
    )
    for selector_field, detail_field, label in location_other_fields:
        selected = str(response.get(selector_field) or "").strip().casefold()
        if selected.startswith(("outro", "outra")) and not _not_empty(response.get(detail_field)):
            warn(
                "localizacao_outro_sem_detalhe",
                f"Informe o nome do outro {label} selecionado.",
                f"{selector_field},{detail_field}",
            )

    doi = str(response.get("doi_adoption") or "").casefold()
    if doi == "não" and _not_empty(response.get("doi_start_year")):
        warn("doi_condicional", "Ano de início do DOI foi informado embora a adoção esteja marcada como Não.", "doi_adoption,doi_start_year")

    interruption = str(response.get("publication_interruption") or "").casefold()
    has_interruption_dates = _not_empty(response.get("interruption_start_date")) or _not_empty(response.get("interruption_end_date"))
    if interruption == "não" and has_interruption_dates:
        warn("interrupcao_condicional", "Datas de interrupção foram informadas embora a resposta seja Não.", "publication_interruption,interruption_start_date,interruption_end_date")

    periodicity = str(response.get("periodicity") or "").casefold()
    if periodicity != "contínua" and _not_empty(response.get("continuous_since_year")):
        warn("fluxo_continuo_condicional", "Ano de fluxo contínuo foi informado para outra periodicidade.", "periodicity,continuous_since_year")

    minimum = response.get("review_time_min_months")
    maximum = response.get("review_time_max_months")
    if isinstance(minimum, (int, float)) and isinstance(maximum, (int, float)) and minimum > maximum:
        warn("faixa_tempo_invertida", "O tempo mínimo de avaliação é maior que o máximo.", "review_time_min_months,review_time_max_months")

    founding_year = response.get("founding_year")
    submission_year = response.get("submission_year")
    if isinstance(founding_year, (int, float)) and isinstance(submission_year, (int, float)) and submission_year < founding_year:
        warn("ano_criacao_invalido", "O ano de criação é posterior ao ano da submissão.", "founding_year,submission_year")

    for field_id in ["respondent_email", "main_email", "alternate_email"]:
        value = response.get(field_id)
        if _not_empty(value) and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", str(value)):
            warn("email_invalido", f"E-mail com formato inválido: {field_id}.", field_id)

    for field_id, value in response.items():
        if field_id.endswith("_url") and _not_empty(value) and not re.match(r"^https?://", str(value), re.IGNORECASE):
            warn("url_invalida", f"URL sem HTTP/HTTPS: {field_id}.", field_id)

    return warnings, attachments

def _yearly_ratio(response: dict[str, Any], numerator_prefix: str) -> list[float | None]:
    ratios: list[float | None] = []
    for suffix in ["y0", "y1", "y2"]:
        numerator = response.get(f"{numerator_prefix}_{suffix}")
        denominator = response.get(f"published_articles_{suffix}")
        if not isinstance(numerator, (int, float)) or not isinstance(denominator, (int, float)) or denominator <= 0:
            ratios.append(None)
        else:
            ratios.append(float(numerator) / float(denominator))
    return ratios

def _deterministic_status(criterion_id: str, response: dict[str, Any]) -> str | None:
    if criterion_id == "PR-2":
        founding = response.get("founding_year")
        submission = response.get("submission_year")
        if isinstance(founding, (int, float)) and isinstance(submission, (int, float)):
            return "Atende" if submission - founding >= 3 else "Não atende"
    if criterion_id == "PR-3":
        answer = str(response.get("publication_interruption") or "").casefold()
        if answer in {"sim", "não"}:
            return "Atende" if answer == "não" else "Não atende"
    if criterion_id == "PR-4":
        access = str(response.get("open_access_free") or "").casefold()
        license_value = str(response.get("cc_license") or "").casefold()
        if access:
            return "Atende" if access == "sim" and license_value not in {"", "não adota"} else "Não atende"
    if criterion_id == "PR-5":
        answer = str(response.get("online_management_system") or "").casefold()
        if answer in {"sim", "não"}:
            return "Atende" if answer == "sim" else "Não atende"
    if criterion_id == "TEC-2.1":
        counts = [response.get(f"published_articles_{suffix}") for suffix in ["y0", "y1", "y2"]]
        if all(isinstance(value, (int, float)) for value in counts):
            if all(value >= 25 for value in counts):
                return "Atende"
            if any(value >= 25 for value in counts):
                return "Atende parcialmente"
            return "Não atende"
    if criterion_id in {"SCI-4.5", "SCI-4.6"}:
        prefix = "international_articles" if criterion_id == "SCI-4.5" else "foreign_language_articles"
        ratios = _yearly_ratio(response, prefix)
        if all(value is not None for value in ratios):
            if all(value >= 0.10 for value in ratios):
                return "Atende"
            if any(value >= 0.10 for value in ratios):
                return "Atende parcialmente"
            return "Não atende"
    return None

def preprocess_criteria(response: dict[str, Any], mappings: list[dict[str, Any]], warnings: list[dict[str, str]]) -> list[dict[str, Any]]:
    warning_fields = {
        field.strip()
        for warning in warnings
        for field in warning.get("field_ids", "").split(",")
        if field.strip()
    }
    criteria: list[dict[str, Any]] = []
    for mapping in mappings:
        field_ids = [value.strip() for value in str(mapping.get("field_ids_de_entrada") or "").split(",") if value.strip()]
        values = {field_id: response.get(field_id) for field_id in field_ids if field_id in response}
        populated = {key: value for key, value in values.items() if _not_empty(value)}
        deterministic = _deterministic_status(str(mapping.get("criterion_id") or ""), response)
        if warning_fields.intersection(field_ids):
            readiness = "Revisão necessária"
        elif not populated:
            readiness = "Dados ausentes"
        else:
            readiness = "Pronto para validação"
        criteria.append(
            {
                "criterion_id": mapping.get("criterion_id"),
                "category": mapping.get("categoria"),
                "code": mapping.get("código"),
                "criterion": mapping.get("critério"),
                "source_field_ids": field_ids,
                "declared_values": populated,
                "validation_method": mapping.get("método_de_validação"),
                "readiness": readiness,
                "preliminary_status": deterministic or "Atende parcialmente",
                "status_basis": "Regra determinística sobre dados declarados" if deterministic else "Declaração recebida; evidência ainda não validada",
                "human_validation_required": True,
            }
        )
    return criteria

def preprocess_response(response: dict[str, Any], schema: dict[str, dict[str, Any]], mappings: list[dict[str, Any]], workbook_dir: Path) -> dict[str, Any]:
    warnings, attachments = validate_response(response, schema, workbook_dir)
    criteria = preprocess_criteria(response, mappings, warnings)
    missing_attachments = sum(not item["exists"] for item in attachments)
    has_missing_other = any(
        warning.get("code") == "localizacao_outro_sem_detalhe" for warning in warnings
    )
    if has_missing_other:
        status = "Pendente de complementação"
    else:
        status = "Revisão necessária" if warnings or missing_attachments else "Pré-processado"
    return {
        "response_id": response.get("response_id"),
        "create_time": response.get("create_time"),
        "last_submitted_time": response.get("last_submitted_time"),
        "journal": response.get("journal_title"),
        "status": status,
        "declared_data": deepcopy(response),
        "attachments": attachments,
        "warnings": warnings,
        "criteria": criteria,
        "processing_policy": {
            "normative_source": "Critérios Educ@ — maio/2026",
            "declared_data_is_evidence": False,
            "human_validation_required": True,
            "external_site_lookup_performed": False,
        },
    }