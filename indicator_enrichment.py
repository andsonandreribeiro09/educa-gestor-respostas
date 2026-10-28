"""Deriva indicadores científicos a partir das planilhas anexadas ao formulário."""

from __future__ import annotations

import csv
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup
from openpyxl import load_workbook


def _normalized(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    plain = "".join(char for char in text if not unicodedata.combining(char)).casefold()
    return re.sub(r"[^a-z0-9]+", "_", plain).strip("_")


def _resolve_attachment(base_dir: Path, declared_path: Any) -> Path | None:
    if not declared_path:
        return None
    raw = Path(str(declared_path))
    candidate = raw if raw.is_absolute() else base_dir / raw
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(base_dir.resolve())
    except (FileNotFoundError, OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def _sheet_rows(path: Path) -> list[tuple[str, list[dict[str, Any]]]]:
    if path.suffix.casefold() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        return [("CSV", [{_normalized(key): value for key, value in row.items()} for row in rows])]
    if path.suffix.casefold() != ".xlsx":
        return []
    workbook = load_workbook(path, read_only=True, data_only=True)
    sheets: list[tuple[str, list[dict[str, Any]]]] = []
    try:
        for worksheet in workbook.worksheets:
            iterator = worksheet.iter_rows(values_only=True)
            header = next(iterator, None)
            if not header:
                continue
            keys = [_normalized(value) for value in header]
            rows = [dict(zip(keys, row)) for row in iterator if any(value not in {None, ""} for value in row)]
            sheets.append((worksheet.title, rows))
    finally:
        workbook.close()
    return sheets


def _first(row: dict[str, Any], aliases: Iterable[str]) -> Any:
    for alias in aliases:
        value = row.get(alias)
        if value not in {None, ""}:
            return value
    return None


def _year(value: Any) -> int | None:
    match = re.search(r"\b(19|20)\d{2}\b", str(value or ""))
    return int(match.group(0)) if match else None


def _article_key(row: dict[str, Any]) -> str | None:
    # O inventário oficial costuma omitir o DOI, enquanto a planilha de autores
    # o contém. O título normalizado é, portanto, a chave comum mais estável.
    value = _first(row, ("titulo_artigo", "titulo", "doi", "url_artigo", "url"))
    return _normalized(value) or None


def _language_from_row(row: dict[str, Any]) -> str | None:
    explicit = _first(row, ("idioma", "lingua", "language", "idioma_artigo"))
    if explicit:
        language = _normalized(explicit)
        return "pt" if language in {"pt", "pt_br", "portugues", "portuguese"} else "foreign"
    url = str(_first(row, ("url_artigo", "url")) or "")
    language = parse_qs(urlparse(url).query).get("ilang", [""])[0].casefold()
    if not language:
        return None
    return "pt" if language.startswith("pt") else "foreign"


def _country_from_row(row: dict[str, Any]) -> str | None:
    country = _first(row, ("pais", "country", "pais_afiliacao"))
    if country:
        return _normalized(country)
    institution = str(_first(row, ("instituicao_principal", "instituicao", "afiliacao")) or "").strip()
    if not institution or "," not in institution:
        return None
    tail = _normalized(institution.rsplit(",", 1)[-1])
    known = {
        "brasil", "brazil", "argentina", "chile", "colombia", "equador", "peru", "uruguai",
        "paraguai", "bolivia", "venezuela", "mexico", "portugal", "espanha", "spain", "franca",
        "france", "estados_unidos", "united_states", "reino_unido", "united_kingdom", "angola",
        "mocambique", "canada", "italia", "italy", "alemanha", "germany", "australia", "cuba",
        "costa_rica",
    }
    return tail if tail in known else None


def _doi_from_rows(article: dict[str, Any], author_rows: list[dict[str, Any]]) -> str | None:
    doi = _first(article, ("doi",))
    if not doi:
        doi = next((_first(row, ("doi",)) for row in author_rows if _first(row, ("doi",))), None)
    value = str(doi or "").strip().removeprefix("https://doi.org/")
    return value or None


def _country_code_from_affiliation(affiliation: str) -> str | None:
    normalized = _normalized(affiliation)
    country_tokens = {
        "brasil": "BR", "brazil": "BR", "portugal": "PT", "franca": "FR", "france": "FR",
        "estados_unidos": "US", "united_states": "US", "angola": "AO", "mocambique": "MZ",
        "mozambique": "MZ", "canada": "CA", "chile": "CL", "argentina": "AR", "uruguai": "UY",
        "spain": "ES", "espanha": "ES", "united_kingdom": "GB", "reino_unido": "GB",
    }
    for token, code in country_tokens.items():
        if token in normalized:
            return code
    known_institutions = {
        "university_of_minho": "PT",
        "universidade_do_minho": "PT",
        "instituto_de_estudos_de_politica_economica": "BR",
        "university_valley_of_taquari": "BR",
        "school_of_government": "BR",
        "unifesp": "BR",
        "caen_ufc": "BR",
        "vale_do_salgado_university_center": "BR",
    }
    for token, code in known_institutions.items():
        if token in normalized:
            return code
    if any(
        token in normalized
        for token in (
            "universidade_federal", "universidade_estadual", "universidade_catolica",
            "universidade_tecnologica_federal", "pontificia_universidade_catolica",
            "federal_university", "university_federal", "state_university", "instituto_federal",
            "federal_institute",
        )
    ) or normalized == "ufc":
        return "BR"
    return None


def _countries_from_openalex(
    articles: list[tuple[int, str, dict[str, Any], list[dict[str, Any]]]],
) -> tuple[dict[tuple[int, str], set[str]], list[str]]:
    """Complementa países ausentes usando DOI/OpenAlex e, por último, a afiliação SciELO."""
    countries: dict[tuple[int, str], set[str]] = defaultdict(set)
    warnings: list[str] = []
    doi_to_keys: dict[str, list[tuple[int, str]]] = defaultdict(list)
    unresolved_pages: dict[tuple[int, str], tuple[str, str | None]] = {}
    for year, key, article, author_rows in articles:
        doi = _doi_from_rows(article, author_rows)
        url = str(_first(article, ("url_artigo", "url")) or "")
        if doi:
            doi_to_keys[doi.casefold()].append((year, key))
        unresolved_pages[(year, key)] = (url, doi)

    headers = {"User-Agent": "EducA-pilot/0.1 (mailto:t_aribeiro@fcc.org.br)"}
    try:
        with httpx.Client(timeout=20, follow_redirects=True, headers=headers) as client:
            dois = list(doi_to_keys)
            for index in range(0, len(dois), 20):
                response = client.get(
                    "https://api.openalex.org/works",
                    params={"filter": f"doi:{'|'.join('https://doi.org/' + doi for doi in dois[index:index + 20])}", "per-page": 100},
                )
                response.raise_for_status()
                for work in response.json().get("results", []):
                    doi = str(work.get("doi") or "").removeprefix("https://doi.org/").casefold()
                    codes = {
                        str(institution.get("country_code") or "").upper()
                        for authorship in work.get("authorships", [])
                        for institution in authorship.get("institutions", [])
                        if institution.get("country_code")
                    }
                    for article_key in doi_to_keys.get(doi, []):
                        countries[article_key].update(codes)

            for article_key, (url, doi) in unresolved_pages.items():
                if countries.get(article_key) or not url:
                    continue
                page = client.get(url)
                page.raise_for_status()
                soup = BeautifulSoup(page.text, "html.parser")
                if not doi:
                    doi_tag = soup.find("meta", attrs={"name": "citation_doi"})
                    doi = str(doi_tag.get("content") or "") if doi_tag else None
                affiliations = [
                    str(tag.get("content") or "").strip()
                    for tag in soup.find_all("meta", attrs={"name": "citation_author_affiliation"})
                    if str(tag.get("content") or "").strip()
                ]
                inferred = [_country_code_from_affiliation(affiliation) for affiliation in dict.fromkeys(affiliations)]
                # Não aceite correspondência aproximada de instituição: nomes
                # genéricos podem apontar para outro país. Só use a página se
                # todas as afiliações puderem ser classificadas com segurança.
                if inferred and all(inferred):
                    countries[article_key].update(str(code) for code in inferred)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        warnings.append(f"A consulta complementar de afiliações ficou indisponível: {exc}")
    return countries, warnings


def derive_indicators(response: dict[str, Any], workbook_dir: Path, *, online: bool = False) -> dict[str, Any]:
    """Retorna sugestões auditáveis; nunca substitui valores digitados pelo usuário."""
    submission_year = _year(response.get("submission_year")) or 2026
    target_years = [submission_year, submission_year - 1, submission_year - 2]
    authors_path = _resolve_attachment(workbook_dir, response.get("authors_file"))
    articles_path = _resolve_attachment(workbook_dir, response.get("editorial_dates_file"))
    warnings: list[str] = []
    sources: list[dict[str, Any]] = []

    inventory: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    if articles_path:
        sheets = _sheet_rows(articles_path)
        preferred = next(((name, rows) for name, rows in sheets if _normalized(name) == "inventario_oficial"), None)
        selected = preferred or (sheets[0] if sheets else None)
        if selected:
            sheet_name, rows = selected
            for row in rows:
                year = _year(_first(row, ("ano", "year", "ano_publicacao")))
                key = _article_key(row)
                if year and key:
                    inventory[year][key] = row
            sources.append({"field": "editorial_dates_file", "path": str(articles_path), "sheet": sheet_name, "rows": len(rows)})
    else:
        warnings.append("A planilha de artigos e datas não foi localizada; os indicadores de idioma não puderam ser calculados.")

    authors: dict[int, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    if authors_path:
        sheets = _sheet_rows(authors_path)
        selected = sheets[0] if sheets else None
        if selected:
            sheet_name, rows = selected
            for row in rows:
                year = _year(_first(row, ("ano", "year", "ano_publicacao")))
                key = _article_key(row)
                if year and key:
                    authors[year][key].append(row)
            sources.append({"field": "authors_file", "path": str(authors_path), "sheet": sheet_name, "rows": len(rows)})
    else:
        warnings.append("A planilha de autores não foi localizada; as afiliações internacionais não puderam ser calculadas.")

    suggested: dict[str, int] = {}
    details: dict[str, dict[str, Any]] = {}
    online_countries: dict[tuple[int, str], set[str]] = {}
    if online and inventory and authors:
        unresolved = []
        for year in target_years:
            for key, article in inventory.get(year, {}).items():
                rows = authors.get(year, {}).get(key, [])
                local_countries = [_country_from_row(row) for row in rows]
                if not rows or not local_countries or any(country is None for country in local_countries):
                    unresolved.append((year, key, article, rows))
        online_countries, online_warnings = _countries_from_openalex(unresolved)
        warnings.extend(online_warnings)
    for offset, year in enumerate(target_years):
        article_rows = inventory.get(year, {})
        language_known = {key: _language_from_row(row) for key, row in article_rows.items()}
        if article_rows and all(language_known.values()):
            field = f"foreign_language_articles_y{offset}"
            suggested[field] = sum(value == "foreign" for value in language_known.values())
            details[field] = {
                "year": year,
                "method": "idioma original registrado no inventário/URL SciELO",
                "coverage": f"{len(language_known)}/{len(article_rows)} artigos",
                "confidence": "confirmado_pela_planilha",
            }
        elif article_rows:
            known = sum(value is not None for value in language_known.values())
            warnings.append(f"Idioma de {len(article_rows) - known} artigo(s) de {year} não identificado; o total desse ano ficou para revisão.")

        expected_keys = set(article_rows) or set(authors.get(year, {}))
        confirmed_international = 0
        completely_classified = 0
        for key in expected_keys:
            rows = authors.get(year, {}).get(key, [])
            countries = [_country_from_row(row) for row in rows]
            external_codes = online_countries.get((year, key), set())
            if any(country not in {None, "brasil", "brazil"} for country in countries) or any(code != "BR" for code in external_codes):
                confirmed_international += 1
                completely_classified += 1
            elif external_codes or (rows and countries and all(country in {"brasil", "brazil"} for country in countries)):
                completely_classified += 1
        field = f"international_articles_y{offset}"
        if expected_keys and completely_classified == len(expected_keys):
            suggested[field] = confirmed_international
            details[field] = {
                "year": year,
                "method": "país das afiliações na planilha, OpenAlex e SciELO; artigos distintos",
                "coverage": f"{completely_classified}/{len(expected_keys)} artigos",
                "confidence": "confirmado_pela_planilha",
            }
        elif expected_keys:
            warnings.append(
                f"Afiliações de {len(expected_keys) - completely_classified} artigo(s) de {year} estão sem país; "
                "o total internacional não foi preenchido para evitar informar zero ou um total incompleto."
            )

    return {"suggested": suggested, "details": details, "warnings": warnings, "sources": sources, "years": target_years}
