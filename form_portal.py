"""Adaptador do formulário original para o componente bidirecional Streamlit."""
import base64
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone, date
from pathlib import Path
from urllib.parse import urlsplit, unquote

import streamlit as st
import streamlit.components.v1 as components
import metrics
import storage
from rules import preprocess_response, validate_response
from identity_validation import validate_identity
from indicator_enrichment import derive_indicators

ROOT=Path(__file__).resolve().parent
SCHEMA=json.loads((ROOT/'data/schema.json').read_text(encoding='utf-8'))
FORM=components.declare_component('educa_form_original',path=str(ROOT/'form_component'))

def coerce(raw):
    response={}
    for field in SCHEMA['fields']:
        key=field['field_id'];value=raw.get(key)
        if isinstance(value,str):value=value.strip()
        if value is None or value=='':response[key]=None;continue
        if field['type']=='inteiro':
            try:
                number=float(str(value).replace(',','.'))
                if number.is_integer():value=int(number)
            except (ValueError,TypeError):pass
        response[key]=value
    response['submission_year']=response.get('submission_year') or date.today().year
    if not response.get('respondent_email'):response['respondent_email']=response.get('main_email')
    return response

def checked_response(raw, uploads):
    response=coerce(raw)
    for key in metrics.FILES:
        path=response.get(key)
        if path and path not in uploads:raise ValueError('Anexo não pertence a este rascunho. Anexe a planilha novamente.')
    return response

def dispatch(message, session, receipt_pdf):
    route=unquote(urlsplit(message['url']).path);body=message.get('body') or {}
    drafts=session.setdefault('drafts',{});uploads=session.setdefault('uploads',{})
    now=datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if route=='/api/uploads':
        filename=Path(str(body['filename']).replace('\\','/')).name;suffix=Path(filename).suffix.lower()
        if suffix not in {'.xlsx','.xls','.csv'}:raise ValueError('Envie uma planilha XLSX, XLS ou CSV.')
        content=base64.b64decode(body['content_b64'],validate=True)
        if len(content)>10*1024*1024:raise ValueError('Cada anexo deve ter até 10 MB.')
        relative='runtime/uploads/'+hashlib.sha256(content).hexdigest()+suffix
        path=metrics.BASE/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(content)
        uploads[relative]={'name':filename,'content':content}
        return {'declared_path':relative,'filename':filename}
    if route=='/api/drafts':
        draft_id=body['draft_id']
        existing=drafts.get(draft_id)
        if existing and existing.get('status')=='submitted':return existing
        result={'draft_id':draft_id,'status':'draft','response':body['response'],'identity':body.get('identity'),'ui_state':{'current_section':body.get('current_section',0),'unlocked_section':body.get('unlocked_section',0),'section_validations':body.get('section_validations',{})},'updated_at':now}
        drafts[draft_id]=result;return result
    if route.startswith('/api/drafts/'):
        key=route.split('/')[3];draft=drafts.get(key)
        if draft is None:raise ValueError('Rascunho não está disponível nesta sessão de teste.')
        if route.endswith('/receipt'):return {'receipt':draft['receipt']}
        return draft
    if route=='/api/identity/lookup':return validate_identity(coerce(body['response']))
    if route=='/api/indicators/derive':return derive_indicators(checked_response(body['response'],uploads),metrics.BASE)
    if route in {'/api/validate-section','/api/validate','/api/submissions'}:
        response=checked_response(body['response'],uploads)
        warnings,_=validate_response(response,SCHEMA['schema'],metrics.BASE)
        warnings=[w for w in warnings if w['field_ids'] not in {'response_id','create_time','last_submitted_time'}]
        if route=='/api/validate-section':
            ids={f['field_id'] for f in SCHEMA['fields'] if f['section']==body['section']}
            warnings=[w for w in warnings if set(w.get('field_ids','').split(','))&ids]
            identity=None
            if body['section']=='Identificação':
                identity=validate_identity(response)
                if not identity.get('verification',{}).get('registry_confirmed') or identity.get('errors'):
                    warnings.append({'code':'identidade_pendente','field_ids':'issn_online','message':'Confirme a identidade do ISSN antes de continuar.'})
            return {'section':body['section'],'can_continue':not warnings,'status':'blocked' if warnings else 'validated','warnings':warnings,'identity':identity,'checked_at':now}
        if route=='/api/validate':return preprocess_response(response,SCHEMA['schema'],SCHEMA['mappings'],metrics.BASE)
        draft=drafts.get(body.get('draft_id'))
        if draft and draft.get('status')=='submitted':return {'accepted':True,'already_submitted':True,'receipt':draft['receipt']}
        if warnings:raise ValueError('Complete os campos obrigatórios e anexos antes do envio: '+', '.join(w['field_ids'] for w in warnings))
        identity=validate_identity(response)
        if not identity.get('verification',{}).get('registry_confirmed') or identity.get('errors'):raise ValueError('Confirme a identidade cadastral do ISSN antes do envio.')
        protocol='WEB-'+datetime.now(timezone.utc).strftime('%Y%m%d')+'-'+uuid.uuid4().hex[:12].upper()
        response.update(response_id=protocol,create_time=now,last_submitted_time=now)
        processed=preprocess_response(response,SCHEMA['schema'],SCHEMA['mappings'],metrics.BASE)
        files={key:uploads[response[key]] for key in metrics.FILES if response.get(key)}
        storage.save(response,processed,files)
        receipt={'receipt_id':protocol,'download_url':'data:application/pdf;base64,'+base64.b64encode(receipt_pdf(response)).decode(),'verification_url':'data:application/json;base64,'+base64.b64encode(json.dumps(response,ensure_ascii=False).encode()).decode(),'email_status':'test_mode','recipient_email':response.get('main_email')}
        if draft:draft.update(status='submitted',receipt=receipt)
        return {'accepted':True,'receipt':receipt,'warnings':processed['warnings']}
    raise ValueError('Operação não disponível nesta versão do portal.')

def render_form(receipt_pdf):
    session=st.session_state.setdefault('original_form_session',{})
    replies=session.setdefault('replies',{})
    request=FORM(bootstrap={k:SCHEMA[k] for k in ('fields','sections','criteria_count')},reply=session.get('reply'),key='original_form')
    if request and request.get('id')!=session.get('last_id'):
        request_id=request['id']
        if request_id not in replies:
            try:replies[request_id]={'id':request_id,'result':dispatch(request,session,receipt_pdf)}
            except Exception as error:replies[request_id]={'id':request_id,'error':str(error) if isinstance(error,ValueError) else 'Não foi possível concluir a operação. Tente novamente.'}
        session['last_id']=request_id;session['reply']=replies[request_id]
        st.rerun()
