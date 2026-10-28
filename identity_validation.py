"""Consulta e comparação de identidade de periódicos por ISSN."""

from __future__ import annotations

import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, Callable
from urllib.parse import quote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup


SOURCE_TIMEOUT = 8.0
CACHE_TTL_SECONDS = 60 * 60
USER_AGENT = "Educa-FCC-Identity-Validator/0.2"
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()


def normalize_issn(value: Any) -> str | None:
    compact = re.sub(r"[^0-9Xx]", "", str(value or ""))
    if len(compact) != 8:
        return None
    normalized = f"{compact[:4]}-{compact[4:].upper()}"
    digits = [10 if char == "X" else int(char) for char in compact.upper()]
    checksum = sum(digit * weight for digit, weight in zip(digits, range(8, 0, -1), strict=True))
    return normalized if checksum % 11 == 0 else None


def _source_result(name: str, url: str, *, status: str, **values: Any) -> dict[str, Any]:
    return {"name": name, "url": url, "status": status, **values}


def _crossref(client: httpx.Client, issn: str) -> dict[str, Any]:
    url = f"https://api.crossref.org/journals/{quote(issn)}"
    try:
        response = client.get(url)
        if response.status_code == 404:
            return _source_result("Crossref", url, status="not_found")
        response.raise_for_status()
        message = response.json().get("message", {})
        titles = message.get("title") or []
        title = str(titles[0]) if isinstance(titles, list) and titles else str(titles or "")
        doi_start_year = None
        first_doi = ""
        doi_history_detail = ""
        try:
            works_response = client.get(
                f"{url}/works",
                params={"sort": "published", "order": "asc", "rows": 1},
            )
            works_response.raise_for_status()
            items = works_response.json().get("message", {}).get("items") or []
            if items:
                first_item = items[0]
                doi_start_year = _crossref_publication_year(first_item)
                first_doi = str(first_item.get("DOI") or "")
                if doi_start_year:
                    doi_history_detail = "Primeiro ano de publicação com DOI localizado na Crossref"
        except (httpx.HTTPError, ValueError, TypeError):
            # A indisponibilidade do histórico de obras não invalida o registro
            # principal do periódico encontrado na Crossref.
            pass
        return _source_result(
            "Crossref",
            url,
            status="found",
            title=title,
            publisher=str(message.get("publisher") or ""),
            issns=message.get("ISSN") or [],
            homepage="",
            doi_start_year=doi_start_year,
            first_doi=first_doi,
            doi_history_detail=doi_history_detail,
        )
    except (httpx.HTTPError, ValueError, TypeError) as error:
        return _source_result("Crossref", url, status="unavailable", detail=str(error))


def _openalex(client: httpx.Client, issn: str) -> dict[str, Any]:
    url = f"https://api.openalex.org/sources/issn:{quote(issn)}"
    try:
        response = client.get(url)
        if response.status_code == 404:
            return _source_result("OpenAlex", url, status="not_found")
        response.raise_for_status()
        message = response.json()
        primary_location = message.get("primary_location") or {}
        return _source_result(
            "OpenAlex",
            url,
            status="found",
            title=str(message.get("display_name") or ""),
            publisher=str(message.get("host_organization_name") or ""),
            issns=message.get("issn") or [],
            homepage=str(message.get("homepage_url") or primary_location.get("landing_page_url") or ""),
            country_code=str(message.get("country_code") or ""),
            in_doaj=bool(message.get("is_oa_in_doaj")),
        )
    except (httpx.HTTPError, ValueError, TypeError) as error:
        return _source_result("OpenAlex", url, status="unavailable", detail=str(error))


def _doaj(client: httpx.Client, issn: str) -> dict[str, Any]:
    url = f"https://doaj.org/api/search/journals/issn:{quote(issn)}?pageSize=1"
    try:
        response = client.get(url)
        response.raise_for_status()
        payload = response.json()
        results = payload.get("results") or []
        if not results:
            return _source_result("DOAJ", url, status="not_found")
        source = results[0].get("_source") or results[0].get("source") or {}
        bibjson = source.get("bibjson") or {}
        links = bibjson.get("link") or []
        homepage = next((str(item.get("url")) for item in links if item.get("type") == "homepage"), "")
        identifiers = bibjson.get("identifier") or []
        issns = [str(item.get("id")) for item in identifiers if item.get("type") in {"pissn", "eissn"}]
        publisher = bibjson.get("publisher") or {}
        return _source_result(
            "DOAJ",
            url,
            status="found",
            title=str(bibjson.get("title") or ""),
            publisher=str(publisher.get("name") or ""),
            issns=issns,
            homepage=homepage,
        )
    except (httpx.HTTPError, ValueError, TypeError) as error:
        return _source_result("DOAJ", url, status="unavailable", detail=str(error))


def _valid_start_year(value: Any) -> int | None:
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    return year if 1800 <= year <= datetime.now().year else None


def _crossref_publication_year(item: dict[str, Any]) -> int | None:
    for field in ("published", "published-online", "published-print", "issued"):
        date_parts = (item.get(field) or {}).get("date-parts") or []
        if date_parts and date_parts[0]:
            year = _valid_start_year(date_parts[0][0])
            if year:
                return year
    return None


def _extract_portal_start_year(text: str) -> int | None:
    patterns = (
        r"dates?\s+of\s+publication\s*:?[\s\u00a0]*((?:18|19|20)\d{2})\s*[-–]",
        r"description\s*:?[\s\u00a0]*(?:no\.?|n[ºo]\.?|number)?\s*1\s*\(?((?:18|19|20)\d{2})\)?",
        r"(?:datas?\s+de\s+publica[cç][aã]o|fecha\s+de\s+publicaci[oó]n)\s*:?[\s\u00a0]*((?:18|19|20)\d{2})\s*[-–]",
    )
    plain = re.sub(r"\s+", " ", text)
    for pattern in patterns:
        match = re.search(pattern, plain, re.I)
        if match:
            return _valid_start_year(match.group(1))
    return None


def _portal_issn(client: httpx.Client, issn: str) -> dict[str, Any]:
    url = f"https://portal.issn.org/resource/ISSN/{quote(issn)}"
    try:
        response = client.get(url, headers={"Accept": "text/html,application/xhtml+xml"})
        if response.status_code == 404:
            return _source_result("Portal ISSN", url, status="not_found")
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")
        text = soup.get_text(" ", strip=True)
        record_issns = list(dict.fromkeys(match.group(1).upper() for match in re.finditer(r"\bISSN(?:-L)?(?:\s+Record)?\s*:?\s*(\d{4}-\d{3}[\dXx])\b", text, re.I)))
        if issn not in record_issns:
            return _source_result("Portal ISSN", url, status="review", detail="O conteúdo retornado não confirmou o identificador pesquisado.")
        year = _extract_portal_start_year(text)
        title = ""
        title_match = re.search(r"key\s+title\s*:?[\s\u00a0]*(.+?)(?=\s+(?:Copy permalink|Identifiers|Publication|Titles)\b)", text, re.I)
        if title_match:
            title = title_match.group(1).strip()
        return _source_result(
            "Portal ISSN",
            url,
            status="found" if title else "review",
            title=title,
            issns=record_issns,
            founding_year=year,
            evidence="Ano de início/data de publicação no registro ISSN" if year else "Registro localizado sem ano extraível automaticamente",
        )
    except (httpx.HTTPError, ValueError, TypeError) as error:
        return _source_result("Portal ISSN", url, status="unavailable", detail=str(error))


def _extract_miguilim_start_year(soup: BeautifulSoup) -> int | None:
    for cell in soup.select(".dc_date_startyear"):
        match = re.search(r"(?:18|19|20)\d{2}", cell.get_text(" ", strip=True))
        if match:
            return _valid_start_year(match.group(0))
    meta = soup.find("meta", attrs={"name": re.compile(r"^DC\.date$", re.I)})
    return _valid_start_year(meta.get("content")) if meta else None


def _miguilim_metadata_value(soup: BeautifulSoup, css_class: str) -> str:
    label = soup.select_one(f"td.metadataFieldLabel1.{css_class}")
    value = label.find_next_sibling("td") if label else None
    return re.sub(r"\s+", " ", value.get_text(" ", strip=True)).strip() if value else ""


def _miguilim(client: httpx.Client, issn: str) -> dict[str, Any]:
    search_url = f"https://miguilim.ibict.br/simple-search?query={quote(issn)}"
    try:
        response = client.get(search_url, headers={"Accept": "text/html,application/xhtml+xml"})
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")
        item_paths = []
        for heading in soup.select("h3[onclick]"):
            match = re.search(r"location\.href\s*=\s*['\"]([^'\"]*?/handle/miguilim/\d+)['\"]", str(heading.get("onclick") or ""), re.I)
            if match:
                if match.group(1) not in item_paths:
                    item_paths.append(match.group(1))
        if not item_paths:
            return _source_result("Miguilim/IBICT", search_url, status="not_found")
        item_soup = None
        record_issns = []
        for item_path in item_paths[:5]:
            item_url = urljoin(search_url, item_path)
            item_response = client.get(item_url, headers={"Accept": "text/html,application/xhtml+xml"})
            item_response.raise_for_status()
            candidate = BeautifulSoup(item_response.text, "lxml")
            identifiers = [str(meta.get("content") or "") for meta in candidate.find_all("meta", attrs={"name": re.compile(r"(?:issn|eissn)$", re.I)})]
            identifiers += [cell.get_text(" ", strip=True) for cell in candidate.select("td.metadataFieldValue[class*='issn']")]
            record_issns = list(dict.fromkeys(normalized for text in identifiers for match in re.finditer(r"\b\d{4}[- ]?\d{3}[\dXx]\b", text) if (normalized := normalize_issn(match.group()))))
            if issn in record_issns:
                item_soup = candidate
                break
        if item_soup is None:
            return _source_result("Miguilim/IBICT", search_url, status="not_found", detail="Resultados descartados: o ISSN pesquisado não foi confirmado nos identificadores dos registros.")
        year = _extract_miguilim_start_year(item_soup)
        title_meta = item_soup.find("meta", attrs={"name": re.compile(r"^DC\.title$", re.I)})
        return _source_result(
            "Miguilim/IBICT",
            item_url,
            status="found",
            title=str(title_meta.get("content") or "") if title_meta else "",
            issns=record_issns,
            founding_year=year,
            year_scope="online" if title_meta and re.search(r"\bonline\b", str(title_meta.get("content") or ""), re.I) else "publication",
            publisher=_miguilim_metadata_value(item_soup, "dc_publisher_name"),
            editor_name=_miguilim_metadata_value(item_soup, "dc_contributor_editor"),
            main_email=_miguilim_metadata_value(item_soup, "dc_identifier_email"),
            postal_code=_miguilim_metadata_value(item_soup, "dc_description_cep"),
            state_region=_miguilim_metadata_value(item_soup, "dc_description_state"),
            city=_miguilim_metadata_value(item_soup, "dc_description_city"),
            phone=_miguilim_metadata_value(item_soup, "dc_description_phone"),
            evidence="Ano de início de publicação informado pelo Miguilim/IBICT" if year else "Registro localizado sem ano extraível automaticamente",
        )
    except (httpx.HTTPError, ValueError, TypeError) as error:
        return _source_result("Miguilim/IBICT", search_url, status="unavailable", detail=str(error))


def _xml_value(xml: str, name: str) -> str:
    match = re.search(rf"<(?:[A-Za-z0-9_-]+:)?{name}\b[^>]*>(.*?)</(?:[A-Za-z0-9_-]+:)?{name}>", xml, re.I | re.S)
    return BeautifulSoup(match.group(1), "html.parser").get_text(" ", strip=True) if match else ""


def _oai_pmh(client: httpx.Client, homepage: str, issn: str) -> dict[str, Any]:
    base = homepage.split("?", 1)[0].rstrip("/")
    endpoint = base if base.casefold().endswith("/oai") else f"{base}/oai"
    url = f"{endpoint}?verb=Identify"
    try:
        response = client.get(url, headers={"Accept": "application/xml,text/xml"})
        if response.status_code in {404, 405}:
            return _source_result("OAI-PMH", url, status="not_found")
        response.raise_for_status()
        xml = response.text
        if not re.search(r"<(?:[A-Za-z0-9_-]+:)?Identify\b", xml, re.I) or re.search(r"<(?:[A-Za-z0-9_-]+:)?error\b", xml, re.I):
            return _source_result("OAI-PMH", url, status="not_found")
        earliest = _xml_value(xml, "earliestDatestamp")
        year_match = re.search(r"(?:19|20)\d{2}", earliest)
        year = _valid_start_year(year_match.group(0)) if year_match else None
        return _source_result(
            "OAI-PMH",
            url,
            status="found",
            title=_xml_value(xml, "repositoryName"),
            issns=[issn],
            homepage=homepage,
            first_record_year=year,
            main_email=_xml_value(xml, "adminEmail"),
        )
    except (httpx.HTTPError, ValueError, TypeError) as error:
        return _source_result("OAI-PMH", url, status="unavailable", detail=str(error))


def _classify_knowledge_area(text: str) -> str:
    plain = _plain_text(text)
    education = bool(re.search(r"\b(educacao|ensino|aprendizagem|pedagog|docent|educador)", plain))
    interdisciplinary = bool(re.search(r"\b(interdisciplinar|multidisciplinar|multitematic|diversas areas|diferentes areas)", plain))
    if education and interdisciplinary:
        return "Interdisciplinar com foco em Educação"
    if education:
        return "Ensino" if "ensino" in plain and "educacao" not in plain else "Educação"
    return ""


def _focus_scope(paragraphs: list[str]) -> str:
    weighted = (
        (r"\bmissao\b", 7),
        (r"\bobjetiv", 6),
        (r"\bfoco\b|\bescopo\b", 7),
        (r"\binterdisciplinar|\bmultidisciplinar", 5),
        (r"\beducacao\b|\bensino\b", 4),
        (r"\brevista\b|\bperiodico\b", 3),
        (r"\bconhecimento cientifico\b|\bproducao academica\b", 3),
    )
    scored: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for index, paragraph in enumerate(paragraphs):
        clean = re.sub(r"\s+", " ", paragraph).strip()
        if not 80 <= len(clean) <= 1_400:
            continue
        plain = _plain_text(clean)
        if plain in seen:
            continue
        seen.add(plain)
        score = sum(weight for pattern, weight in weighted if re.search(pattern, plain))
        if re.search(r"\b(editor|manuscrit|parecer|rejeitar|conflito de interesse|discriminacao)\b", plain):
            score -= 8
        if score >= 6:
            scored.append((score, -index, clean))
    selected = [item[2] for item in sorted(scored, reverse=True)[:2]]
    return " ".join(selected)[:1_500]


def _extract_site_start_year(text: str) -> int | None:
    plain = re.sub(r"\s+", " ", text)
    patterns = (
        r"(?:publica|publicada|circula|editada)\s+(?:ininterruptamente\s+)?desde\s+((?:18|19|20)\d{2})",
        r"(?:criada|fundada|lan[cç]ada|iniciada)\s+(?:em\s+)?((?:18|19|20)\d{2})",
        r"(?:no\.?|n[ºo]\.?|n[uú]mero)\s*1\s*\(?((?:18|19|20)\d{2})\)?",
        r"((?:18|19|20)\d{2})\s*[-–]\s*(?:18|19|20)\d{2}\s*[-–:]?\s*(?:anual|semestral|quadrimestral|trimestral|bimestral|mensal)",
    )
    candidates: list[int] = []
    for pattern in patterns:
        for match in re.finditer(pattern, plain, re.I):
            year = _valid_start_year(match.group(1))
            if year:
                candidates.append(year)
    return min(candidates) if candidates else None


def _explicit_founding_history(paragraphs: list[str], title: str) -> tuple[int | None, str]:
    """Only explicit journal history can supersede an existing founding year."""
    journal_name = _plain_text(re.split(r"\s*[|–—]\s*", title)[0])
    pattern = re.compile(
        r"(?:criad[ao]|fundad[ao]|lan[cç]ad[ao]|iniciad[ao]|founded|established)"
        r"\s+(?:(?:em|in)\s+)?((?:18|19|20)\d{2})", re.I,
    )
    evidence: list[tuple[int, str]] = []
    for paragraph in paragraphs:
        if not journal_name or journal_name not in _plain_text(paragraph):
            continue
        for match in pattern.finditer(paragraph):
            year = _valid_start_year(match.group(1))
            if year:
                evidence.append((year, paragraph))
    # Conflicting explicit histories must not be silently resolved by choosing a minimum.
    if len({year for year, _ in evidence}) != 1:
        return None, ""
    return evidence[0] if evidence else (None, "")


def _official_site(client: httpx.Client, homepage: str, issn: str) -> dict[str, Any]:
    base = homepage.split("?", 1)[0].rstrip("/")
    urls = [base]
    pages: list[tuple[str, BeautifulSoup]] = []
    errors: list[str] = []
    visited: set[str] = set()
    while urls:
        url = urls.pop(0)
        if url in visited:
            continue
        visited.add(url)
        try:
            response = client.get(url, headers={"Accept": "text/html,application/xhtml+xml"})
            if response.status_code == 404:
                continue
            response.raise_for_status()
            resolved = str(response.url).split("?", 1)[0].rstrip("/")
            pages.append((resolved, BeautifulSoup(response.text, "lxml")))
            journal_base = resolved
            for suffix in ("/about/editorialteam", "/about/contact", "/about", "/index"):
                if journal_base.casefold().endswith(suffix):
                    journal_base = journal_base[: -len(suffix)]
                    break
            for suffix in ("/about", "/about/contact", "/about/editorialTeam"):
                enrichment_url = f"{journal_base}{suffix}"
                if enrichment_url not in visited and enrichment_url not in urls:
                    urls.append(enrichment_url)
        except (httpx.HTTPError, ValueError) as error:
            errors.append(str(error))
    if not pages:
        return _source_result("Site oficial", homepage, status="unavailable", detail="; ".join(errors))

    paragraphs: list[str] = []
    all_text: list[str] = []
    for _, soup in pages:
        for unwanted in soup.select("script, style, noscript, nav, footer"):
            unwanted.decompose()
        all_text.append(soup.get_text(" ", strip=True))
        paragraphs.extend(element.get_text(" ", strip=True) for element in soup.select("p, article li, main li"))
    combined = " ".join(all_text)
    founding_year = _extract_site_start_year(combined)
    address = ""
    editor_name = ""
    publisher = ""
    phone = ""
    for _, soup in pages:
        if not address and (node := soup.select_one(".page-contact .address, .contact-section .address")):
            address = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
        if not editor_name and (node := soup.select_one(".contact-primary .contact-name")):
            editor_name = re.sub(r"\s+-\s+.*$", "", node.get_text(" ", strip=True)).strip()
        if not publisher and (node := soup.select_one(".contact-primary .contact-affiliation")):
            publisher = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
        if not phone and (node := soup.select_one(".contact-primary .contact-phone .value")):
            phone = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
    title = ""
    if pages[0][1].title:
        title = re.sub(r"\s+", " ", pages[0][1].title.get_text(" ", strip=True)).strip()
    history_year, history_excerpt = _explicit_founding_history(paragraphs, title)
    if history_year:
        founding_year = history_year
    return _source_result(
        "Site oficial",
        pages[-1][0],
        status="found",
        title=title,
        issns=[issn],
        homepage=homepage,
        knowledge_area=_classify_knowledge_area(combined),
        focus_scope=_focus_scope(paragraphs),
        founding_year=founding_year,
        founding_year_basis="explicit_history" if history_year else "publication_start",
        history_excerpt=history_excerpt,
        editor_name=editor_name,
        publisher=publisher,
        phone=phone,
        postal_address=address,
        evidence="Histórico ou periodicidade no site oficial" if founding_year else "",
    )


def _latindex(issn: str) -> dict[str, Any]:
    # A busca pública usa Cloudflare Turnstile. O piloto oferece a conferência
    # oficial ao revisor, sem tentar contornar a proteção anti-automação.
    return _source_result(
        "Latindex",
        "https://www.latindex.org/latindex/",
        status="manual",
        detail=f"Pesquisar o ISSN {issn} no Diretório ou no Catálogo 2.0.",
    )


def _cached_lookup(issn: str) -> dict[str, Any] | None:
    with _CACHE_LOCK:
        cached = _CACHE.get(issn)
        if cached and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]
    return None


def lookup_issn(issn: str) -> dict[str, Any]:
    cached = _cached_lookup(issn)
    if cached:
        return cached

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    with httpx.Client(headers=headers, timeout=SOURCE_TIMEOUT, follow_redirects=True) as client:
        lookups: list[Callable[[], dict[str, Any]]] = [
            lambda: _crossref(client, issn),
            lambda: _openalex(client, issn),
            lambda: _doaj(client, issn),
        ]
        with ThreadPoolExecutor(max_workers=len(lookups)) as executor:
            sources = list(executor.map(lambda lookup: lookup(), lookups))
        homepage = _best_source_value([source for source in sources if source.get("status") == "found"], "homepage")
        enrichment: list[Callable[[], dict[str, Any]]] = [
            lambda: _portal_issn(client, issn),
            lambda: _miguilim(client, issn),
        ]
        if homepage:
            enrichment.extend([lambda: _oai_pmh(client, homepage, issn), lambda: _official_site(client, homepage, issn)])
        with ThreadPoolExecutor(max_workers=len(enrichment)) as executor:
            sources.extend(executor.map(lambda lookup: lookup(), enrichment))
    sources.append(_latindex(issn))
    result = {"issn": issn, "sources": sources}
    with _CACHE_LOCK:
        _CACHE[issn] = (time.monotonic(), result)
    return result


def _plain_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", ascii_text.casefold()).strip()


def _host(value: Any) -> str:
    try:
        host = (urlparse(str(value or "")).hostname or "").casefold()
    except ValueError:
        return ""
    return host.removeprefix("www.")


def _best_source_value(sources: list[dict[str, Any]], field: str) -> str:
    for source in sources:
        value = str(source.get(field) or "").strip()
        if source.get("status") == "found" and value:
            return value
    return ""


def _preferred_source_value(sources: list[dict[str, Any]], field: str, source_names: tuple[str, ...]) -> str:
    for name in source_names:
        value = _best_source_value([source for source in sources if source.get("name") == name], field)
        if value:
            return value
    return _best_source_value(sources, field)


def _country_from_sources(sources: list[dict[str, Any]]) -> str:
    country_names = {
        "AR": "Argentina", "BO": "Bolívia", "BR": "Brasil", "CL": "Chile",
        "CO": "Colômbia", "CR": "Costa Rica", "CU": "Cuba", "DE": "Alemanha",
        "EC": "Equador", "ES": "Espanha", "FR": "França", "GB": "Reino Unido",
        "IT": "Itália", "MX": "México", "PE": "Peru", "PT": "Portugal",
        "PY": "Paraguai", "US": "Estados Unidos", "UY": "Uruguai", "VE": "Venezuela",
    }
    country = _preferred_source_value(sources, "country", ("Miguilim/IBICT", "Site oficial", "OpenAlex"))
    if country:
        return country
    code = _preferred_source_value(sources, "country_code", ("OpenAlex",)).upper()
    return country_names.get(code, code)


def _founding_year_consensus(sources: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]], bool]:
    evidence: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for source in sources:
        year = _valid_start_year(source.get("founding_year"))
        if source.get("status") == "found" and year:
            key = (source.get("name"), source.get("url"), year)
            if key in seen:
                continue
            seen.add(key)
            evidence.append({
                "source": source.get("name"), "year": year, "url": source.get("url"),
                "basis": source.get("founding_year_basis", "publication_start"),
                "scope": source.get("year_scope", "publication"),
            })
    if not evidence:
        return "", [], False
    # Journal creation and the first year of an online edition are distinct events.
    # Prefer explicit history over bibliographic publication-start dates.
    history = [item for item in evidence if item["source"] == "Site oficial" and item["basis"] == "explicit_history"]
    if history:
        years = {item["year"] for item in history}
        return (str(next(iter(years))) if len(years) == 1 else ""), evidence, len(years) > 1
    publication = [item for item in evidence if item["scope"] != "online"]
    if not publication:
        return "", evidence, False
    counts: dict[int, int] = {}
    for item in publication:
        counts[item["year"]] = counts.get(item["year"], 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    top_year, top_count = ranked[0]
    conflict = len(counts) > 1 and (top_count == 1 or len([count for _, count in ranked if count == top_count]) > 1)
    return ("" if conflict else str(top_year)), evidence, len(counts) > 1


def clear_identity_cache(values: dict[str, Any]) -> None:
    with _CACHE_LOCK:
        for key in ("issn_online", "issn_print"):
            _CACHE.pop(normalize_issn(values.get(key)), None)


def validate_identity(values: dict[str, Any]) -> dict[str, Any]:
    raw_issns = [values.get("issn_online"), values.get("issn_print")]
    supplied = [str(value).strip() for value in raw_issns if str(value or "").strip()]
    valid = [normalize_issn(value) for value in supplied]
    invalid = [value for value, normalized in zip(supplied, valid, strict=True) if normalized is None]
    normalized_issns = list(dict.fromkeys(value for value in valid if value))

    if not supplied:
        return {
            "status": "missing",
            "can_continue": False,
            "errors": ["Informe ao menos um ISSN para iniciar a checagem de identidade."],
            "warnings": [],
            "sources": [],
            "matches": [],
        }
    if invalid:
        return {
            "status": "invalid",
            "can_continue": False,
            "errors": [f"ISSN inválido: {value}. Confira os oito caracteres e o dígito verificador." for value in invalid],
            "warnings": [],
            "sources": [],
            "matches": [],
        }

    lookups = [lookup_issn(issn) for issn in normalized_issns]
    sources = [source for lookup in lookups for source in lookup["sources"]]
    registries = {"Crossref", "OpenAlex", "DOAJ", "Portal ISSN", "Miguilim/IBICT"}
    for source in sources:
        if source.get("name") in registries and source.get("status") == "found":
            returned = {normalize_issn(item) for item in source.get("issns", [])}
            if not returned.intersection(normalized_issns):
                source["status"] = "review"
                source["detail"] = "O registro não confirmou o ISSN consultado; seus dados não foram utilizados."
    found = [source for source in sources if source.get("status") == "found"]
    confirmed = [source for source in found if source.get("name") in registries]
    confirmed_issns = {normalize_issn(item) for source in confirmed for item in source.get("issns", [])}
    registry_confirmed = all(issn in confirmed_issns for issn in normalized_issns)
    warnings: list[str] = []
    matches: list[dict[str, Any]] = []

    declared_title = str(values.get("journal_title") or "").strip()
    source_title = _best_source_value(found, "title")
    if declared_title and source_title:
        similarity = SequenceMatcher(None, _plain_text(declared_title), _plain_text(source_title)).ratio()
        title_status = "match" if similarity >= 0.82 else "mismatch"
        matches.append({"field_id": "journal_title", "status": title_status, "declared": declared_title, "source_value": source_title})
        if title_status == "mismatch":
            warnings.append("O título informado diverge do título localizado nas fontes consultadas.")

    declared_site = str(values.get("journal_site_url") or "").strip()
    source_site = _best_source_value(found, "homepage")
    if declared_site and source_site:
        site_status = "match" if _host(declared_site) == _host(source_site) else "review"
        matches.append({"field_id": "journal_site_url", "status": site_status, "declared": declared_site, "source_value": source_site})
        if site_status == "review":
            warnings.append("O domínio do site informado difere do endereço encontrado; confirme se houve mudança de plataforma.")

    if not registry_confirmed:
        warnings.append("Verificação cadastral pendente. Confira o ISSN informado; se estiver correto, tente novamente quando as fontes estiverem disponíveis. Os demais campos permanecem bloqueados até a confirmação.")

    founding_year, founding_year_evidence, year_disagreement = _founding_year_consensus(found)
    doi_start_year = _best_source_value(found, "doi_start_year")
    publisher = _preferred_source_value(found, "publisher", ("Miguilim/IBICT", "Site oficial", "OpenAlex", "Crossref", "DOAJ"))
    if year_disagreement:
        warnings.append("As fontes consultadas apresentam anos de início diferentes; o ano exige conferência humana.")
    verified_year = bool(founding_year) and not year_disagreement and any(
        item["source"] == "Site oficial" and item["basis"] == "explicit_history"
        and str(item["year"]) == founding_year for item in founding_year_evidence
    )

    return {
        "status": "verified" if registry_confirmed and not warnings else "review",
        "can_continue": registry_confirmed,
        "issns": normalized_issns,
        "errors": [],
        "warnings": warnings,
        "sources": sources,
        "matches": matches,
        "verification": {"checksum_valid": True, "registry_confirmed": registry_confirmed, "confirmed_sources": [source["name"] for source in confirmed]},
        "verified_fields": ["founding_year"] if verified_year else [],
        "suggested": {
            "journal_title": source_title,
            "journal_site_url": source_site,
            "publisher_institution": publisher,
            "editor_name": _preferred_source_value(found, "editor_name", ("Miguilim/IBICT", "Site oficial")),
            "city": _preferred_source_value(found, "city", ("Miguilim/IBICT", "Site oficial")),
            "state_region": _preferred_source_value(found, "state_region", ("Miguilim/IBICT", "Site oficial")),
            "country": _country_from_sources(found),
            "phone": _preferred_source_value(found, "phone", ("Miguilim/IBICT", "Site oficial")),
            "postal_address": _preferred_source_value(found, "postal_address", ("Site oficial", "Miguilim/IBICT")),
            "knowledge_area": _best_source_value(found, "knowledge_area"),
            "focus_scope": _best_source_value(found, "focus_scope"),
            "founding_year": founding_year,
            "doi_start_year": doi_start_year,
            "main_email": _preferred_source_value(found, "main_email", ("Miguilim/IBICT", "Site oficial")),
            "founding_year_evidence": founding_year_evidence,
        },
    }

