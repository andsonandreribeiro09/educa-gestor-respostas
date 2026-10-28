const state = {
  fields: [],
  sections: [],
  values: {},
  currentSection: 0,
  validation: null,
  identity: null,
  identityLoading: false,
  sectionValidations: {},
  unlockedSection: 0,
  draftId: null,
  submitting: false,
  advancing: false,
  generation: 0,
  issnEditingField: null,
  issnComplementField: null,
  lookupSequence: 0,
  indicatorDerivation: null,
  indicatorsLoading: false,
};

const $ = (selector) => document.querySelector(selector);
const formCard = $("#form-card");
const sectionNav = $("#section-nav");
const previousButton = $("#previous-button");
const nextButton = $("#next-button");
const submitButton = $("#submit-button");
const validateButton = $("#validate-button");
const draftStorageKey = "educa-form-draft-id-v2";
let draftSaveTimer;
let draftSaveChain = Promise.resolve();

function createDraftId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `draft-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function ensureDraftId() {
  if (!state.draftId) {
    state.draftId = createDraftId();
    localStorage.setItem(draftStorageKey, state.draftId);
  }
  return state.draftId;
}

function setDraftStatus(message, tone = "saved") {
  const status = $("#draft-status");
  if (!status) return;
  status.className = `draft-status ${tone}`;
  status.textContent = message;
}

function hasDraftContent() {
  return Object.values(state.values).some((value) => String(value ?? "").trim() !== "");
}

function draftPayload() {
  return {
    draft_id: ensureDraftId(),
    response: state.values,
    identity: state.identity,
    current_section: state.currentSection,
    unlocked_section: state.unlockedSection,
    section_validations: state.sectionValidations,
  };
}

function saveDraft() {
  clearTimeout(draftSaveTimer);
  if (!hasDraftContent()) return draftSaveChain;
  const payload = JSON.parse(JSON.stringify(draftPayload()));
  const savingDraftId = payload.draft_id;
  setDraftStatus("Salvando rascunho…", "saving");
  draftSaveChain = draftSaveChain.catch(() => undefined).then(async () => {
    try {
      const saved = await request("/api/drafts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      const savedAt = new Date(saved.updated_at).toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
      if (state.draftId === savingDraftId) setDraftStatus(`Rascunho salvo · ${savedAt}`, "saved");
      return saved;
    } catch (error) {
      if (state.draftId === savingDraftId) setDraftStatus("Rascunho não salvo", "error");
      throw error;
    }
  });
  return draftSaveChain;
}

function scheduleDraftSave(delay = 800) {
  if (state.submitting) return;
  if (!hasDraftContent()) return;
  setDraftStatus("Alterações pendentes", "pending");
  clearTimeout(draftSaveTimer);
  draftSaveTimer = setTimeout(() => { saveDraft().catch(() => undefined); }, delay);
}

async function restoreSavedDraft() {
  const requestedDraftId = new URLSearchParams(window.location.search).get("draft");
  const savedId = requestedDraftId || localStorage.getItem(draftStorageKey);
  if (!savedId) {
    ensureDraftId();
    return false;
  }
  state.draftId = savedId;
  try {
    const draft = await request(`/api/drafts/${encodeURIComponent(savedId)}`);
    if (draft.status === "submitted") {
      const completed = await request(`/api/drafts/${encodeURIComponent(savedId)}/receipt`);
      resetForNewSubmission();
      showReceipt(completed.receipt);
      return false;
    }
    state.values = draft.response || {};
    clearSchemaPlaceholders(state.values);
    state.identity = draft.identity || null;
    const ui = draft.ui_state || {};
    state.currentSection = Math.min(Number(ui.current_section || 0), Math.max(0, state.sections.length - 1));
    state.unlockedSection = Math.min(Number(ui.unlocked_section || 0), Math.max(0, state.sections.length - 1));
    state.sectionValidations = ui.section_validations || {};
    state.validation = state.sectionValidations[state.currentSection] || null;
    localStorage.setItem(draftStorageKey, savedId);
    setDraftStatus("Rascunho recuperado", "saved");
    return true;
  } catch (error) {
    if (requestedDraftId) throw error;
    localStorage.removeItem(draftStorageKey);
    state.draftId = null;
    ensureDraftId();
    return false;
  }
}

const indicatorFieldLabels = {
  international_articles_y0: "autoria internacional no ano da submissão",
  international_articles_y1: "autoria internacional no ano anterior",
  international_articles_y2: "autoria internacional há dois anos",
  foreign_language_articles_y0: "língua estrangeira no ano da submissão",
  foreign_language_articles_y1: "língua estrangeira no ano anterior",
  foreign_language_articles_y2: "língua estrangeira há dois anos",
};

async function deriveScientificIndicators({ notify = true } = {}) {
  if (state.indicatorsLoading || (!state.values.authors_file && !state.values.editorial_dates_file)) return [];
  state.indicatorsLoading = true;
  try {
    const result = await request("/api/indicators/derive", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ response: state.values, draft_id: state.draftId }),
    });
    state.indicatorDerivation = result;
    const filled = [];
    Object.entries(result.suggested || {}).forEach(([fieldId, value]) => {
      if (String(state.values[fieldId] ?? "").trim() !== "") return;
      state.values[fieldId] = value;
      filled.push(indicatorFieldLabels[fieldId] || fieldId);
    });
    if (filled.length) {
      scheduleDraftSave(0);
      if (notify) showToast(`Indicadores preenchidos pelos anexos: ${filled.join(", ")}.`);
    } else if (notify && (result.warnings || []).length) {
      showToast("Os anexos foram lidos, mas faltam países ou idiomas para calcular alguns indicadores com segurança.");
    }
    return filled;
  } catch (error) {
    if (notify) showToast(error.message);
    return [];
  } finally {
    state.indicatorsLoading = false;
  }
}

const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

function normalize(value) {
  return String(value ?? "").trim().toLocaleLowerCase("pt-BR");
}

function clearSchemaPlaceholders(values) {
  if (normalize(values?.country) === "lista iso de países") delete values.country;
}

function visible(field) {
  if (!field.condition) return true;
  const [source, expected] = field.condition.split("=");
  return normalize(state.values[source]) === normalize(expected);
}

function clearInactiveDependents(parentFieldId) {
  state.fields
    .filter((field) => field.condition.startsWith(`${parentFieldId}=`) && !visible(field))
    .forEach((field) => { delete state.values[field.field_id]; });
}

function renderKeepingPosition(fieldId, scrollTop) {
  render();
  const replacement = document.querySelector(`[data-field="${fieldId}"]`);
  if (replacement) replacement.focus({ preventScroll: true });
  window.scrollTo({ top: scrollTop, behavior: "instant" });
  requestAnimationFrame(() => window.scrollTo({ top: scrollTop, behavior: "instant" }));
}

function fieldsForSection(section) {
  return state.fields.filter((field) => field.section === section && visible(field));
}

function issnLookupReady() {
  const supplied = [state.values.issn_online, state.values.issn_print].filter((value) => String(value ?? "").trim());
  return supplied.length > 0 && supplied.every((value) => compactIssn(value).length === 8);
}

function compactIssn(value) {
  return String(value ?? "").toUpperCase().replace(/[^0-9X]/g, "");
}

function updateIssnField(fieldId, rawValue) {
  const value = formatIssn(rawValue);
  const previous = compactIssn(state.values[fieldId]);
  const next = compactIssn(value);
  if (previous === next) {
    state.values[fieldId] = value;
    return false;
  }
  const knownIssns = new Set([
    ...(state.identity?.issns || []),
    ...(state.identity?.sources || []).flatMap((source) => source.issns || []),
  ].map(compactIssn));
  const complementCandidate = !previous || state.issnComplementField === fieldId;
  const complement = complementCandidate && next.length === 8 && knownIssns.has(next);
  const addingComplement = complementCandidate && next.length < 8 && ["issn_online", "issn_print"].some((key) => key !== fieldId && compactIssn(state.values[key]).length === 8);
  if (addingComplement) state.issnComplementField = fieldId;
  else state.issnComplementField = null;
  const hasJournalData = Object.entries(state.values).some(([key, item]) => !["issn_online", "issn_print"].includes(key) && String(item ?? "").trim());
  const editingNewIdentity = state.issnEditingField === fieldId && !state.identity;
  const changedJournal = !complement && !addingComplement && !editingNewIdentity && (previous.length === 8 || state.identity || hasJournalData);
  if (changedJournal) {
    resetForNewSubmission();
    state.issnEditingField = fieldId;
  }
  state.values[fieldId] = value;
  return Boolean(changedJournal);
}

function identityConfirmed() {
  const supplied = [state.values.issn_online, state.values.issn_print].map(compactIssn).filter(Boolean);
  const checked = new Set((state.identity?.issns || []).map(compactIssn));
  return !state.identityLoading && Boolean(state.identity?.verification?.registry_confirmed) && !(state.identity?.errors || []).length && supplied.length > 0 && supplied.every(issn => issn.length === 8 && checked.has(issn));
}

function syncIdentityGate() {
  const locked = !identityConfirmed();
  formCard.querySelectorAll('[data-field], [data-multi-field]').forEach(control => {
    const key = control.dataset.field || control.dataset.multiField;
    if (!["issn_online", "issn_print"].includes(key)) control.disabled = locked;
  });
  renderNav();
  renderActions();
}

function identityPanelContent() {
  const lockNotice = '<p class="identity-lock-notice" role="status">Os demais campos e o avanço ficam bloqueados até a confirmação cadastral do ISSN.</p>';
  if (state.identityLoading) {
    return `<div class="identity-compact pending"><span class="identity-spinner" aria-hidden="true"></span><strong>Validando ISSN…</strong></div>${lockNotice}`;
  }
  if (!state.identity) {
    return `<div class="identity-compact pending"><span class="validation-icon">○</span><strong>Informe o ISSN para liberar o formulário</strong></div>${lockNotice}`;
  }

  const sourceDetails = identitySourceDetails(state.identity);
  const errors = state.identity.errors || [];
  const warnings = state.identity.warnings || [];
  if (errors.length) {
    return `<div class="identity-compact invalid"><span class="validation-icon">×</span><strong>ISSN inválido · confira o número informado</strong></div>${lockNotice}${sourceDetails}`;
  }
  if (!identityConfirmed()) {
    return `<div class="identity-compact review"><span class="validation-icon">!</span><strong>Verificação cadastral pendente</strong></div>${lockNotice}<p>Confira o ISSN. Se ele estiver correto e a consulta estiver indisponível, tente novamente mais tarde.</p><button type="button" id="retry-identity" class="button">Consultar ISSN novamente</button>${sourceDetails}`;
  }
  if (warnings.length) {
    return `<div class="identity-compact review"><span class="validation-icon">!</span><strong>Dígito válido · identidade requer revisão</strong></div>${sourceDetails}`;
  }
  if (!state.identity.verification?.registry_confirmed) {
    return `<div class="identity-compact review"><span class="validation-icon">!</span><strong>Identidade ainda não confirmada</strong></div>${sourceDetails}`;
  }
  return `<div class="identity-compact verified"><span class="validation-icon">✓</span><strong>ISSN localizado em fonte cadastral</strong></div>${sourceDetails}`;
}

function identitySourceDetails(identity) {
  const labels = {found:"ISSN correspondente localizado",not_found:"Não localizado",unavailable:"Consulta indisponível",manual:"Conferência manual",review:"Registro sem correspondência confirmada"};
  const items = (identity.sources || []).map((source) => {
    const name = escapeHtml(source.name);
    const link = /^https?:\/\//i.test(source.url || "") ? `<a href="${escapeHtml(source.url)}" target="_blank" rel="noopener noreferrer">${name}</a>` : name;
    const record = source.status === "found" ? ` — ${escapeHtml(source.title || "")}${source.issns?.length ? ` (${escapeHtml(source.issns.join(", "))})` : ""}` : "";
    return `<li>${link}: ${escapeHtml(labels[source.status] || "Pendente")}${record}</li>`;
  }).join("");
  return `<section class="identity-sources" aria-label="Fontes da identificação"><strong>Como este ISSN foi conferido?</strong><p>O dígito verificador é uma checagem matemática. A identidade depende da correspondência do ISSN no registro consultado; isso não aprova o periódico nem valida toda esta etapa.</p><ul>${items || "<li>Nenhuma fonte confirmou a identidade.</li>"}</ul></section>`;
}

function renderIdentityPanel() {
  const panel = $("#identity-panel");
  if (panel) panel.innerHTML = identityPanelContent();
  document.querySelector('#retry-identity')?.addEventListener?.('click', lookupIdentity);
  syncIdentityGate();
}

const identityAutofillFields = {
  journal_title: "nome da revista",
  journal_site_url: "site principal",
  publisher_institution: "instituição editora",
  editor_name: "editor responsável",
  city: "cidade",
  state_region: "estado ou região",
  country: "país",
  phone: "telefone",
  postal_address: "endereço institucional",
  issn_print: "ISSN impresso",
  knowledge_area: "área de conhecimento",
  focus_scope: "foco e escopo",
  founding_year: "ano de criação",
  doi_start_year: "ano de início da adoção do DOI",
  main_email: "e-mail principal",
};

function applyIdentitySuggestions(identity, previousIdentity = null) {
  if ((identity?.errors || []).length) return [];
  const filled = [];
  Object.entries(identity?.suggested || {}).forEach(([fieldId, suggestedValue]) => {
    if (!(fieldId in identityAutofillFields)) return;
    if (fieldId === "doi_start_year" && normalize(state.values.doi_adoption) !== "sim") return;
    const currentValue = String(state.values[fieldId] ?? "").trim();
    const previousSuggestion = String(previousIdentity?.suggested?.[fieldId] ?? "").trim();
    if (!String(suggestedValue ?? "").trim()) return;
    if (normalize(currentValue) === normalize(suggestedValue)) return;
    const verifiedYear = fieldId === "founding_year" && identity.verified_fields?.includes(fieldId);
    if (!verifiedYear && currentValue && (!previousSuggestion || normalize(currentValue) !== normalize(previousSuggestion))) return;

    state.values[fieldId] = suggestedValue;
    const control = document.querySelector(`[data-field="${fieldId}"]`);
    if (control) {
      control.value = formatInputValue(suggestedValue, control.type);
      control.classList.add("auto-filled");
    }
    filled.push(identityAutofillFields[fieldId]);
  });
  return filled;
}

function inputType(field) {
  const type = normalize(field.type);
  if (type === "email") return "email";
  if (type === "url") return "url";
  if (type === "inteiro" || type === "decimal") return "number";
  if (type === "data") return "date";
  if (type === "data hora" || type === "data_hora") return "datetime-local";
  return "text";
}

function formatInputValue(value, type) {
  if (value == null) return "";
  if (type === "datetime-local") return String(value).slice(0, 16);
  if (type === "date") return String(value).slice(0, 10);
  return String(value);
}

function numericAttributes(field) {
  const attributes = [`step="${escapeHtml(field.step ?? "any")}"`, 'inputmode="numeric"'];
  if (field.min !== undefined && field.min !== null) attributes.push(`min="${escapeHtml(field.min)}"`);
  if (field.max !== undefined && field.max !== null) attributes.push(`max="${escapeHtml(field.max)}"`);
  return attributes.join(" ");
}

function textAttributes(field) {
  const attributes = [];
  if (field.placeholder) attributes.push(`placeholder="${escapeHtml(field.placeholder)}"`);
  if (field.pattern) attributes.push(`pattern="${escapeHtml(field.pattern)}"`);
  if (field.maxlength) attributes.push(`maxlength="${escapeHtml(field.maxlength)}"`);
  if (["issn_online", "issn_print"].includes(field.field_id)) attributes.push('inputmode="text"', 'autocomplete="off"');
  return attributes.join(" ");
}

function formatIssn(value) {
  const compact = String(value ?? "").toUpperCase().replace(/[^0-9X]/g, "").slice(0, 8);
  return compact.length > 4 ? `${compact.slice(0, 4)}-${compact.slice(4)}` : compact;
}

function isLongText(field) {
  return ["texto longo", "texto_longo"].includes(normalize(field.type));
}

function isMultipleChoice(field) {
  return ["multipla escolha", "multipla_escolha"].includes(normalize(field.type));
}

function selectedChoices(value) {
  return String(value ?? "")
    .split(/[;,]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function uploadedFileName(value) {
  const parts = String(value ?? "").split(/[\\/]/);
  const storedName = parts.at(-1) || "arquivo anexado";
  return storedName.replace(/^\d{8}-\d{6}-[A-Za-z0-9]+-/, "");
}

function fieldMarkup(field) {
  const value = state.values[field.field_id];
  const required = field.required === "Sim" || field.required === "Condicional";
  const wide = isLongText(field) || isMultipleChoice(field) || ["arquivo", "url"].includes(normalize(field.type)) ? " field--wide" : "";
  const type = inputType(field);
  let control = "";
  if (normalize(field.type) === "arquivo") {
    const attached = String(value ?? "").trim() !== "";
    control = `<div class="upload-row ${attached ? "has-file" : "missing-file"}"><input class="upload-input" id="${field.field_id}" data-field="${field.field_id}" type="file" accept=".xlsx,.xls,.csv" /><label class="upload-button" for="${field.field_id}">${attached ? "Substituir arquivo" : "Escolher arquivo"}</label><span class="file-state">${attached ? `<span class="file-check" aria-hidden="true">✓</span><span><strong>Arquivo anexado</strong><small>${escapeHtml(uploadedFileName(value))}</small></span>` : '<span><strong>Nenhum arquivo anexado</strong><small>Selecione a planilha preenchida.</small></span>'}</span></div>`;
  } else if (isLongText(field)) {
    control = `<textarea id="${field.field_id}" data-field="${field.field_id}">${escapeHtml(value)}</textarea>`;
  } else if (type === "number" && /^(received|approved|rejected)_articles_y[012]$/.test(field.field_id)) {
    control = `<input id="${field.field_id}" data-field="${field.field_id}" type="text" value="${escapeHtml(formatInputValue(value, type))}" list="${field.field_id}-availability" pattern="[0-9]+|Não informada" placeholder="Quantidade ou Não informada" /><datalist id="${field.field_id}-availability"><option value="Não informada"></option></datalist>`;
  } else if (type === "number") {
    control = `<input id="${field.field_id}" data-field="${field.field_id}" type="number" value="${escapeHtml(formatInputValue(value, type))}" ${numericAttributes(field)} placeholder="Digite a quantidade" />`;
  } else if (isMultipleChoice(field)) {
    const selected = selectedChoices(value);
    const selectableOptions = [...field.options];
    selected.forEach((option) => {
      if (!selectableOptions.some((candidate) => normalize(candidate) === normalize(option))) selectableOptions.push(option);
    });
    const choices = selectableOptions.map((option, index) => {
      const checked = selected.some((candidate) => normalize(candidate) === normalize(option));
      return `<label class="choice-option" for="${field.field_id}-${index}"><input id="${field.field_id}-${index}" data-multi-field="${field.field_id}" type="checkbox" value="${escapeHtml(option)}" ${checked ? "checked" : ""} /><span>${escapeHtml(option)}</span></label>`;
    }).join("");
    control = `<div id="${field.field_id}" class="choice-group" role="group" aria-label="${escapeHtml(field.label)}">${choices}</div>`;
  } else if (field.options.length) {
    const selectableOptions = [...field.options];
    if (String(value ?? "").trim() && !selectableOptions.some((option) => normalize(option) === normalize(value))) selectableOptions.unshift(String(value));
    const options = [`<option value="">Selecione…</option>`, ...selectableOptions.map((option) => `<option value="${escapeHtml(option)}" ${normalize(value) === normalize(option) ? "selected" : ""}>${escapeHtml(option)}</option>`)].join("");
    control = `<select id="${field.field_id}" data-field="${field.field_id}">${options}</select>`;
  } else {
    control = `<input id="${field.field_id}" data-field="${field.field_id}" type="${type}" value="${escapeHtml(formatInputValue(value, type))}" ${textAttributes(field)} />`;
  }
  const importanceBadge = Number(field.importance) === 1
    ? `<span class="importance-badge" title="${escapeHtml(`Rastreado em ${field.importance_source || "Modelo de critérios"}`)}">Importância 1</span>`
    : "";
  const indicatorDetail = state.indicatorDerivation?.details?.[field.field_id];
  const automaticNote = indicatorDetail
    ? `<small class="automatic-note">✓ Preenchido pelos anexos · ${escapeHtml(indicatorDetail.year)} · ${escapeHtml(indicatorDetail.coverage)}</small>`
    : "";
  const helpMarkup = field.help && normalize(field.type) === "arquivo" && /\.xlsx$/i.test(field.help)
    ? `<a class="template-link" href="./templates/${encodeURIComponent(field.help)}" download>↓ Baixar modelo · ${escapeHtml(field.help)}</a>`
    : field.help ? `<small>${escapeHtml(field.help)}</small>` : "";
  return `<div class="field${wide}" data-wrapper="${field.field_id}"><label for="${field.field_id}"><span>${escapeHtml(field.label)}${required ? '<span class="required">*</span>' : ""}</span>${importanceBadge}</label>${control}${helpMarkup}${automaticNote}</div>`;
}

function renderNav() {
  sectionNav.innerHTML = state.sections.map((section, index) => {
    const validation = state.sectionValidations[index];
    const complete = Boolean(validation?.can_continue);
    const locked = index > state.unlockedSection || (index > 0 && !identityConfirmed());
    return `<button class="section-link ${index === state.currentSection ? "active" : ""}" data-section-index="${index}" type="button" ${index === state.currentSection ? 'aria-current="step"' : ""} ${locked ? "disabled" : ""}><span class="section-number">${index + 1}</span><span>${escapeHtml(section)}</span><span class="section-check">${complete ? "✓" : ""}</span></button>`;
  }).join("");
  sectionNav.querySelectorAll("[data-section-index]").forEach((button) => button.addEventListener("click", () => {
    if (button.disabled) return;
    state.currentSection = Number(button.dataset.sectionIndex);
    state.validation = state.sectionValidations[state.currentSection] || null;
    render();
    scheduleDraftSave();
  }));
}

function renderSection() {
  const section = state.sections[state.currentSection];
  const fields = fieldsForSection(section);
  const identityPanel = section === "Identificação" ? `<section id="identity-panel" class="identity-panel">${identityPanelContent()}</section>` : "";
  formCard.innerHTML = `<header class="form-header"><span class="eyebrow">Etapa ${state.currentSection + 1} de ${state.sections.length}</span><h2>${escapeHtml(section)}</h2><p>${fields.length} campos nesta etapa. ${state.currentSection === 0 ? "Valide a identificação para continuar." : "Você pode continuar o rascunho e completar as pendências antes do envio."}</p></header>${identityPanel}<div class="field-grid">${fields.map(fieldMarkup).join("")}</div>`;
  document.querySelector('#retry-identity')?.addEventListener?.('click', lookupIdentity);
  syncIdentityGate();
  formCard.querySelectorAll("[data-field]").forEach((control) => {
    const suggestedValue = state.identity?.suggested?.[control.dataset.field];
    if (String(suggestedValue ?? "").trim() && normalize(state.values[control.dataset.field]) === normalize(suggestedValue)) control.classList.add("auto-filled");
    if (state.indicatorDerivation?.details?.[control.dataset.field]) control.classList.add("auto-filled");
    if (control.type === "file") {
      control.addEventListener("change", () => uploadFile(control));
    } else {
      control.addEventListener("input", () => {
        const scrollTop = window.scrollY;
        const issnField = ["issn_online", "issn_print"].includes(control.dataset.field);
        const changedJournal = issnField ? updateIssnField(control.dataset.field, control.value) : false;
        if (issnField) control.value = formatIssn(control.value);
        else state.values[control.dataset.field] = control.value;
        clearInactiveDependents(control.dataset.field);
        invalidateCurrentSection();
        if (["issn_online", "issn_print"].includes(control.dataset.field)) scheduleIdentityLookup();
        if (control.dataset.field === "doi_adoption") {
          if (normalize(control.value) === "sim" && state.identity) {
            const filled = applyIdentitySuggestions(state.identity);
            if (filled.length) showToast(`Preenchido automaticamente: ${filled.join(", ")}.`);
            if (!filled.length && !String(state.values.doi_start_year ?? "").trim() && issnLookupReady()) scheduleIdentityLookup();
          } else if (normalize(control.value) !== "sim") {
            delete state.values.doi_start_year;
          }
        }
        scheduleDraftSave();
        updateSummary();
        if (changedJournal || state.fields.some((field) => field.condition.startsWith(`${control.dataset.field}=`))) {
          renderKeepingPosition(control.dataset.field, scrollTop);
        }
      });
    }
  });
  formCard.querySelectorAll("[data-multi-field]").forEach((control) => {
    control.addEventListener("input", () => {
      const fieldId = control.dataset.multiField;
      const checked = [...formCard.querySelectorAll(`[data-multi-field="${fieldId}"]:checked`)]
        .map((item) => item.value);
      state.values[fieldId] = checked.join(", ");
      invalidateCurrentSection();
      scheduleDraftSave();
      updateSummary();
    });
  });
}

function updateSummary() {
  const visibleFields = state.fields.filter((field) => field.section !== "Controle" && visible(field) && ["Sim", "Condicional"].includes(field.required));
  const filled = visibleFields.filter((field) => String(state.values[field.field_id] ?? "").trim() !== "").length;
  const percent = visibleFields.length ? Math.round((filled / visibleFields.length) * 100) : 0;
  $("#progress-value").textContent = `${percent}%`;
  $("#progress-ring").style.background = `conic-gradient(var(--blue) ${percent}%, #e8edf4 ${percent}%)`;
  $("#progress-copy").textContent = `${filled} de ${visibleFields.length} campos obrigatórios preenchidos`;
  $("#mobile-percent").textContent = `${percent}%`;
  $("#mobile-section").textContent = state.sections[state.currentSection] || "Formulário";
  $("#summary-title").textContent = state.values.journal_title || "Nova submissão";
  $("#warning-count").textContent = state.validation ? state.validation.warnings.length : "—";
}

function renderValidation() {
  const box = $("#validation-result");
  if (!state.validation) {
    box.className = "validation-result empty";
    box.textContent = state.currentSection === 0
      ? "Valide a identificação para liberar o preenchimento."
      : "Valide esta etapa para conferir os campos. Você também pode continuar o rascunho e completar as pendências antes do envio.";
    if (state.currentSection === state.sections.length - 1 && !allSectionsValidated()) {
      box.textContent = "O envio aguarda a validação de todas as etapas. Volte às etapas com pendências para completar os campos e anexos obrigatórios.";
    }
    return;
  }
  const warnings = state.validation.warnings || [];
  box.className = `validation-result ${warnings.length ? "warning" : "success"}`;
  box.innerHTML = warnings.length
    ? `<strong>${state.validation.can_continue ? "Etapa validada com revisão" : state.currentSection === 0 ? "Corrija a identificação para continuar" : "Pendências nesta etapa"}</strong>${!state.validation.can_continue && state.currentSection > 0 ? "<p>Você pode continuar o rascunho e completar estes itens antes do envio.</p>" : ""}<ul class="warning-list">${warnings.slice(0, 10).map((warning) => `<li>${escapeHtml(warning.message)}</li>`).join("")}</ul>${warnings.length > 10 ? `<small>Mais ${warnings.length - 10} alertas nesta etapa.</small>` : ""}`
    : `<strong>Etapa validada.</strong><p>A próxima etapa foi liberada.</p>`;
  if (state.currentSection === state.sections.length - 1 && !allSectionsValidated()) {
    const pending = state.sections.filter((section, index) => !state.sectionValidations[index]?.can_continue);
    box.innerHTML += `<p><strong>Envio pendente:</strong> valide as etapas ${pending.map(escapeHtml).join(", ")}.</p>`;
  }
}

function allSectionsValidated() {
  return state.sections.length > 0 && state.sections.every((section, index) => state.sectionValidations[index]?.can_continue === true);
}

function renderActions() {
  previousButton.disabled = state.currentSection === 0;
  const last = state.currentSection === state.sections.length - 1;
  const currentValidation = state.sectionValidations[state.currentSection];
  nextButton.classList.toggle("hidden", last);
  submitButton.classList.toggle("hidden", !last);
  nextButton.disabled = state.advancing || !identityConfirmed() || (state.currentSection === 0 && !currentValidation?.can_continue);
  nextButton.textContent = state.currentSection > 0 && !currentValidation?.can_continue ? "Continuar rascunho" : "Próxima etapa";
  nextButton.title = state.currentSection > 0 && !currentValidation?.can_continue ? "Salva os dados e continua. As pendências permanecem obrigatórias para o envio final." : "";
  submitButton.disabled = state.submitting || state.advancing || !identityConfirmed() || !allSectionsValidated();
  validateButton.disabled = !identityConfirmed();
  submitButton.textContent = state.submitting ? "Enviando…" : "Enviar para a fila";
  validateButton.textContent = "Validar esta etapa";
}

function render() {
  // Durante a consulta de atualização, preserve a etapa do rascunho. Só
  // reinicie a navegação quando houver uma identidade definitivamente inválida.
  if (!state.identityLoading && !identityConfirmed()) {
    state.currentSection = 0;
    state.unlockedSection = 0;
    state.sectionValidations = {};
    state.validation = null;
  }
  renderNav();
  renderSection();
  renderActions();
  updateSummary();
  renderValidation();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function invalidateCurrentSection() {
  delete state.sectionValidations[state.currentSection];
  if (state.currentSection === 0) state.unlockedSection = 0;
  state.validation = null;
  renderNav();
  renderActions();
  renderValidation();
}

async function goToNextSection() {
  if (state.advancing || !identityConfirmed()) return;
  const sectionIndex = state.currentSection;
  const generation = state.generation;
  const validation = state.sectionValidations[sectionIndex];
  if (sectionIndex === 0 && !validation?.can_continue) {
    showToast("Valide a identificação antes de continuar.");
    return;
  }
  if (sectionIndex >= state.sections.length - 1) return;
  state.advancing = true;
  renderActions();
  try {
    await saveDraft();
    if (generation !== state.generation || sectionIndex !== state.currentSection || !identityConfirmed()) return;
    state.currentSection = sectionIndex + 1;
    state.unlockedSection = Math.max(state.unlockedSection, state.currentSection);
    state.validation = state.sectionValidations[state.currentSection] || null;
    render();
    await saveDraft();
    if (generation === state.generation && !validation?.can_continue) {
      showToast("Rascunho salvo. Complete as pendências desta etapa antes do envio final.");
    }
  } catch (error) {
    showToast(error.message);
  } finally {
    state.advancing = false;
    renderActions();
  }
}

function scheduleIdentityLookup() {
  clearTimeout(scheduleIdentityLookup.timer);
  if (!issnLookupReady()) {
    state.identity = null;
    state.identityLoading = false;
    renderIdentityPanel();
    return;
  }
  state.identityLoading = true;
  renderIdentityPanel();
  scheduleIdentityLookup.timer = setTimeout(lookupIdentity, 700);
}

async function lookupIdentity() {
  const generation = state.generation;
  const sequence = ++state.lookupSequence;
  const lookupKey = `${state.values.issn_online || ""}|${state.values.issn_print || ""}`;
  const stillCurrent = () => generation === state.generation && sequence === state.lookupSequence && lookupKey === `${state.values.issn_online || ""}|${state.values.issn_print || ""}`;
  state.identityLoading = true;
  renderIdentityPanel();
  try {
    const result = await request("/api/identity/lookup?refresh=true", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ response: state.values }) });
    const currentKey = `${state.values.issn_online || ""}|${state.values.issn_print || ""}`;
    if (stillCurrent() && lookupKey === currentKey) {
      const previousIdentity = state.identity;
      state.identity = result;
      state.issnEditingField = null;
      const filled = applyIdentitySuggestions(result, previousIdentity);
      if (filled.length) {
        updateSummary();
        showToast(`Preenchido automaticamente: ${filled.join(", ")}.`);
        scheduleDraftSave(0);
      }
      if (state.sections[state.currentSection] === "Identificação" && state.sectionValidations[state.currentSection]) {
        await validate();
      }
    }
  } catch (error) {
    if (stillCurrent()) state.identity = { errors: [], warnings: [error.message], sources: [] };
  } finally {
    if (stillCurrent()) {
      state.identityLoading = false;
      renderIdentityPanel();
    }
  }
}

async function request(url, options = {}) { return portalRequest(url, options); }

async function uploadFile(control) {
  const generation = state.generation;
  const file = control.files[0];
  if (!file) return;
  const data = new FormData();
  data.append("file", file);
  control.disabled = true;
  try {
    const result = await request("/api/uploads", { method: "POST", body: data });
    if (generation !== state.generation) return;
    state.values[control.dataset.field] = result.declared_path;
    showToast(`${result.filename} anexado.`);
    if (["authors_file", "editorial_dates_file"].includes(control.dataset.field)) {
      await deriveScientificIndicators({ notify: false });
    }
    scheduleDraftSave(0);
    render();
  } catch (error) {
    showToast(error.message);
  } finally {
    control.disabled = false;
  }
}

async function validate() {
  if (!identityConfirmed()) { showToast("Confirme o ISSN antes de preencher e validar as etapas."); return; }
  const generation = state.generation;
  const sectionIndex = state.currentSection;
  const lookupKey = `${state.values.issn_online || ""}|${state.values.issn_print || ""}`;
  const stillCurrent = () => generation === state.generation && sectionIndex === state.currentSection && lookupKey === `${state.values.issn_online || ""}|${state.values.issn_print || ""}`;
  validateButton.disabled = true;
  try {
    await saveDraft();
    if (!stillCurrent()) return;
    const section = state.sections[state.currentSection];
    const result = await request("/api/validate-section", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ section, response: state.values, draft_id: state.draftId, identity: state.identity }) });
    if (!stillCurrent()) return;
    state.sectionValidations[state.currentSection] = result;
    state.validation = result;
    if (result.identity) {
      const previousIdentity = state.identity;
      state.identity = result.identity;
      applyIdentitySuggestions(result.identity, previousIdentity);
    }
    if (result.can_continue) state.unlockedSection = Math.max(state.unlockedSection, Math.min(state.currentSection + 1, state.sections.length - 1));
    scheduleDraftSave(0);
    render();
    showToast(result.can_continue ? "Etapa validada. A próxima etapa foi liberada." : "Corrija os campos indicados para continuar.");
  } catch (error) {
    showToast(error.message);
  } finally {
    renderActions();
  }
}

async function submit() {
  if (state.submitting) return;
  if (!identityConfirmed()) { showToast("Envio bloqueado: confirme o ISSN em uma fonte cadastral."); return; }
  if (!allSectionsValidated()) { showToast("Envio bloqueado: complete os campos e anexos obrigatórios e valide todas as etapas."); return; }
  state.submitting = true;
  clearTimeout(draftSaveTimer);
  clearTimeout(scheduleIdentityLookup.timer);
  formCard.inert = true;
  renderActions();
  try {
    await saveDraft();
    const result = await request("/api/submissions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ response: state.values, draft_id: state.draftId, identity: state.identity }) });
    resetForNewSubmission();
    render();
    showReceipt(result.receipt);
    showToast(`Submissão ${result.receipt.receipt_id} recebida com sucesso.`);
  } catch (error) {
    showToast(error.message);
  } finally {
    state.submitting = false;
    formCard.inert = false;
    renderActions();
  }
}

function resetForNewSubmission() {
  clearTimeout(draftSaveTimer);
  clearTimeout(scheduleIdentityLookup.timer);
  state.generation += 1;
  state.values = {};
  state.currentSection = 0;
  state.unlockedSection = 0;
  state.validation = null;
  state.identity = null;
  state.identityLoading = false;
  state.issnEditingField = null;
  state.issnComplementField = null;
  state.sectionValidations = {};
  state.indicatorDerivation = null;
  state.indicatorsLoading = false;
  state.draftId = null;
  localStorage.removeItem(draftStorageKey);
  const url = new URL(window.location.href);
  url.searchParams.delete("draft");
  window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
  ensureDraftId();
  setDraftStatus("Nova resposta em branco", "saved");
}

function showReceipt(receipt) {
  const modal = $("#receipt-modal");
  $("#receipt-protocol").textContent = receipt.receipt_id;
  $("#receipt-download").href = receipt.download_url;
  $("#receipt-verify").href = receipt.verification_url;
  const status = $("#receipt-email-status");
  if (receipt.email_status === "sent") {
    status.textContent = `Comprovante enviado para ${receipt.recipient_email}.`;
  } else if (receipt.email_status === "recipient_missing") {
    status.textContent = "Comprovante gerado. Informe um e-mail válido da revista para o envio automático.";
  } else if (receipt.email_status === "failed" || receipt.email_status === "test_recipient_blocked") {
    status.textContent = `Comprovante gerado. O e-mail não foi enviado: ${receipt.email_message || "verifique a configuração de envio."}`;
  } else {
    status.textContent = `Comprovante de teste gerado. Nenhum e-mail foi enviado.`;
  }
  modal.classList.remove("hidden");
}

function closeReceipt() {
  $("#receipt-modal").classList.add("hidden");
  formCard.querySelector("input, select, textarea")?.focus({ preventScroll: true });
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("show"), 3500);
}

async function boot() {
  try {
    const data = await request("/api/bootstrap");
    state.fields = data.fields;
    state.sections = data.sections;
    await restoreSavedDraft();
    await deriveScientificIndicators({ notify: false });
    $("#criteria-count").textContent = data.criteria_count;
    previousButton.addEventListener("click", () => { state.currentSection = Math.max(0, state.currentSection - 1); render(); scheduleDraftSave(); });
    nextButton.addEventListener("click", goToNextSection);
    validateButton.addEventListener("click", validate);
    submitButton.addEventListener("click", submit);
    $("#receipt-close").addEventListener("click", closeReceipt);
    $("#receipt-new").addEventListener("click", closeReceipt);
    $("#receipt-modal").addEventListener("click", (event) => {
      if (event.target.id === "receipt-modal") closeReceipt();
    });
    render();
    if (issnLookupReady()) scheduleIdentityLookup();
  } catch (error) {
    formCard.innerHTML = `<div class="validation-result warning"><strong>Não foi possível carregar o formulário.</strong><p>${escapeHtml(error.message)}</p></div>`;
  }
}

boot();
