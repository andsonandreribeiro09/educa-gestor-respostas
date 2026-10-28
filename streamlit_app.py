"""Portal Educ@ de testes: formulário e painel sobre a mesma base."""
from __future__ import annotations
import base64
import hashlib
import io
import json
import re
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import streamlit as st
from reportlab.pdfgen import canvas
from reportlab.lib.utils import simpleSplit
import metrics
import storage
from rules import preprocess_response, validate_response

ROOT = Path(__file__).resolve().parent
SCHEMA = json.loads((ROOT/'data/schema.json').read_text(encoding='utf-8'))
FIELDS = SCHEMA['fields']
SECTIONS = SCHEMA['sections']
st.set_page_config(page_title='Educ@ · Formulário e gestão',page_icon='📚',layout='wide')
st.markdown('<style>.block-container{padding-top:1.5rem;max-width:1600px}div[data-testid="stMetric"]{background:white;padding:16px;border-radius:8px}h1{font-family:Georgia,serif;color:#7e2940}</style>',unsafe_allow_html=True)

def setting(key, default=''):
    try:
        return st.secrets.get(key,default)
    except (FileNotFoundError,st.errors.StreamlitSecretNotFoundError):
        return default

def normalize_issn(value):
    compact=re.sub(r'[^0-9Xx]','',str(value or '')).upper()
    if len(compact)!=8 or not re.fullmatch(r'\d{7}[\dX]',compact):return None
    digits=[10 if c=='X' else int(c) for c in compact]
    return compact[:4]+'-'+compact[4:] if sum(d*w for d,w in zip(digits,range(8,0,-1)))%11==0 else None

@st.cache_data(ttl=3600,show_spinner=False)
def lookup_issn(issn):
    try:
        result=httpx.get('https://api.crossref.org/journals/'+issn,timeout=15,headers={'User-Agent':'Educa-Portal-Test/1.0'})
        if result.status_code==200:
            message=result.json()['message'];title=message.get('title') or ''
            return {'confirmed':True,'title':title[0] if isinstance(title,list) else title,'source':'Crossref'}
        return {'confirmed':False,'message':'ISSN não confirmado na Crossref. Confira o número ou tente novamente.'}
    except (httpx.HTTPError,ValueError,KeyError):return {'confirmed':False,'message':'Consulta cadastral indisponível. Tente novamente mais tarde.'}

def values():
    response=dict(st.session_state.get('draft_values',{}))
    response['submission_year']=date.today().year
    saved_files=st.session_state.setdefault('draft_files',{})
    for field in FIELDS:
        key=field['field_id'];widget='value_'+key;value=st.session_state.get(widget)
        if field['type']=='arquivo':
            upload=st.session_state.get('upload_'+key)
            if upload:
                content=upload.getvalue();digest=hashlib.sha256(content).hexdigest()
                saved_files[key]={'name':Path(upload.name).name,'content':content}
                response[key]=f'runtime/uploads/{digest}{Path(upload.name).suffix.lower()}'
        elif widget in st.session_state:
            if value is None or value=='':response.pop(key,None)
            else:response[key]=', '.join(value) if isinstance(value,list) else value.isoformat() if isinstance(value,(datetime,date)) else value
    st.session_state['draft_values']=dict(response)
    return response

def active(field,response):
    condition=field.get('condition','')
    if not condition:return True
    key,expected=condition.split('=',1)
    return str(response.get(key,'')).casefold()==expected.strip().casefold()

def collect_files(response):
    files={};folder=metrics.BASE/'runtime/uploads';folder.mkdir(parents=True,exist_ok=True)
    for field in metrics.FILES:
        upload=st.session_state.get('draft_files',{}).get(field)
        if not upload:continue
        content=upload['content']
        if len(content)>10*1024*1024:raise ValueError('Cada anexo deve ter até 10 MB.')
        suffix=Path(upload['name']).suffix.lower()
        if suffix not in {'.xlsx','.csv','.xls'}:raise ValueError('Envie anexos XLSX, XLS ou CSV.')
        path=folder/(hashlib.sha256(content).hexdigest()+suffix)
        path.write_bytes(content)
        response[field]=str(path.relative_to(metrics.BASE)).replace('\\','/')
        files[field]={'name':Path(upload['name']).name,'content':content}
    return files

def field_input(field):
    key=field['field_id'];kind=field['type'];label=field['label']+(' *' if field['required']=='Sim' else '')
    if key=='submission_year':
        st.caption(f'Ano de referência: {date.today().year}. O ano corrente é parcial.');return
    kwargs={'key':'value_'+key,'help':field.get('help') or None}
    saved=st.session_state.get('draft_values',{}).get(key)
    if kwargs['key'] not in st.session_state and saved is not None and kind!='arquivo':
        if kind=='multipla_escolha':saved=[v.strip() for v in str(saved).split(',') if v.strip()]
        if kind=='data':saved=date.fromisoformat(saved)
        if kind=='inteiro' and re.fullmatch(r'(received|approved|rejected)_articles_y[012]',key):saved=str(saved)
        st.session_state[kwargs['key']]=saved
    if kind=='arquivo':
        st.file_uploader(label,type=['xlsx','xls','csv'],key='upload_'+key,help='Até 10 MB. Use somente dados de teste nesta fase.')
        template=ROOT/'templates'/str(field.get('help',''))
        if template.is_file() and template.suffix=='.xlsx':st.download_button('Baixar modelo da planilha',template.read_bytes(),file_name=template.name,key='template_'+key)
        cached=st.session_state.get('draft_files',{}).get(key)
        if cached:
            st.caption('Anexo guardado nesta sessão: '+cached['name'])
            st.button('Remover anexo',key='remove_'+key,on_click=remove_file,args=(key,))
    elif kind in {'sim_nao','lista'}:
        st.selectbox(label,field['options'],index=None,placeholder='Selecione…',**kwargs)
    elif kind=='multipla_escolha':
        st.multiselect(label,field['options'],**kwargs)
    elif kind=='texto_longo':
        st.text_area(label,height=120,**kwargs)
    elif kind=='inteiro':
        if re.fullmatch(r'(received|approved|rejected)_articles_y[012]',key):
            st.text_input(label,placeholder='Quantidade ou Não informada',**kwargs)
        else:
            st.number_input(label,min_value=int(field.get('min',0)),max_value=int(field.get('max',9999)),value=None,step=1,**kwargs)
    elif kind=='data':st.date_input(label,value=None,format='DD/MM/YYYY',**kwargs)
    else:st.text_input(label,**kwargs)

def normalized_response():
    response=values()
    if response.get('main_email'):response['respondent_email']=response['main_email']
    for field in FIELDS:
        key=field['field_id']
        if not active(field,response):response.pop(key,None)
        if field['type']=='inteiro' and key in response:
            raw=response[key]
            if isinstance(raw,str) and raw.strip().isdigit():response[key]=int(raw)
            elif isinstance(raw,str) and raw.casefold()=='não informada':response[key]='Não informada'
    return response

def pdf_receipt(receipt):
    stream=io.BytesIO();pdf=canvas.Canvas(stream);pdf.setTitle('Educ@ - Comprovante de teste')
    pdf.setFont('Helvetica-Bold',18);pdf.drawString(50,790,'Educ@ - Recebimento de submissão de teste')
    pdf.setFont('Helvetica',10);y=750
    for text in [receipt['response_id'],receipt['journal_title'],'Recebida em '+receipt['create_time'],'Este comprovante confirma o registro no ambiente de teste.','A aprovação depende de avaliação humana.','Não equivale a envio ao Google Forms ou ao Portal Educ@ institucional.']:
        for line in simpleSplit(text,'Helvetica',10,490):pdf.drawString(50,y,line);y-=17
        y-=10
    pdf.save();return stream.getvalue()

def new_form():
    for key in list(st.session_state):
        if key.startswith(('value_','upload_')) or key in {'draft_values','draft_files','last_receipt','submission_token','identity','identity_issn','test_consent'}:del st.session_state[key]
    st.session_state['form_section']=0

def remove_file(key):
    st.session_state.get('draft_files',{}).pop(key,None)
    st.session_state.get('draft_values',{}).pop(key,None)
    st.session_state.pop('upload_'+key,None)

def form_page():
    st.title('Educ@ · Solicitação de avaliação')
    st.caption('FUNDAÇÃO CARLOS CHAGAS · Formulário de teste em 12 etapas')
    st.info('Ambiente de teste: use dados fictícios. O envio aparece no painel do gestor desta aplicação e não envia e-mails nem registra uma submissão institucional.')
    st.warning('Neste piloto, os envios ficam no armazenamento temporário do Streamlit. O gestor deve exportar o backup; reinícios podem apagar os novos registros.')
    if st.session_state.get('last_receipt'):
        receipt=st.session_state['last_receipt'];st.success('Submissão de teste recebida. Protocolo: '+receipt['response_id'])
        st.download_button('Baixar comprovante PDF',pdf_receipt(receipt),file_name=receipt['response_id']+'.pdf',mime='application/pdf')
        st.download_button('Baixar cópia das respostas',json.dumps(receipt,ensure_ascii=False,indent=2).encode(),file_name=receipt['response_id']+'.json',mime='application/json')
        st.button('Preencher outra revista',on_click=new_form,type='primary');return
    if 'form_section' not in st.session_state:st.session_state['form_section']=0
    index=st.session_state['form_section']
    section=st.selectbox('Etapa do formulário',range(len(SECTIONS)),format_func=lambda i:f'{i+1:02d}. {SECTIONS[i]}',key='form_section')
    response=normalized_response()
    st.progress((section+1)/len(SECTIONS),text=f'Etapa {section+1} de {len(SECTIONS)} · {SECTIONS[section]}')
    st.subheader(SECTIONS[section])
    for field in FIELDS:
        if field['section']==SECTIONS[section] and active(field,response):field_input(field)
    if section==0:
        if st.button('Validar ISSN em fonte cadastral'):
            issn=normalize_issn(values().get('issn_online'))
            if not issn:st.error('ISSN inválido. Confira os oito caracteres e o dígito verificador.')
            else:
                with st.spinner('Consultando Crossref…'):identity=lookup_issn(issn)
                st.session_state['identity']=identity;st.session_state['identity_issn']=issn
        identity=st.session_state.get('identity',{})
        if identity.get('confirmed'):st.success('ISSN confirmado na Crossref: '+identity.get('title',''))
        elif identity:st.error(identity.get('message','Identidade pendente.'))
    st.caption('Os valores ficam nesta sessão enquanto você navega pelas etapas. Campos marcados com * são obrigatórios.')
    if section<len(SECTIONS)-1:
        st.button('Continuar para a próxima etapa',on_click=lambda:st.session_state.update(form_section=section+1),type='primary')
    else:
        st.checkbox('Confirmo que este envio é apenas um teste.',key='test_consent')
        if st.button('Enviar submissão de teste',type='primary'):
            response=normalized_response()
            for key in ['received','approved','rejected']:
                for y in range(3):
                    field=f'{key}_articles_y{y}';v=response.get(field)
                    if v is not None and not isinstance(v,int) and v!='Não informada':st.error('Use quantidade inteira ou Não informada nas estatísticas editoriais.');return
            try:files=collect_files(response)
            except ValueError as error:st.error(str(error));return
            warnings,_=validate_response(response,SCHEMA['schema'],metrics.BASE)
            # Os controles de sistema são atribuídos pelo servidor.
            warnings=[w for w in warnings if w.get('field_ids') not in {'response_id','create_time','last_submitted_time'}]
            if warnings:
                labels={f['field_id']:f['label'] for f in FIELDS}
                st.error('Revise as pendências antes de enviar:')
                for warning in warnings:
                    st.write('• '+labels.get(warning['field_ids'],warning['message']))
                return
            if not st.session_state.get('test_consent'):st.error('Confirme que este é um envio de teste.');return
            issn=normalize_issn(response.get('issn_online'))
            if not issn or issn!=st.session_state.get('identity_issn') or not st.session_state.get('identity',{}).get('confirmed'):
                st.error('Valide o ISSN na primeira etapa antes de enviar.');return
            token=st.session_state.setdefault('submission_token','WEB-'+datetime.now(timezone.utc).strftime('%Y%m%d')+'-'+uuid.uuid4().hex[:12].upper())
            now=datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            response.update(response_id=token,create_time=now,last_submitted_time=now)
            processed=preprocess_response(response,SCHEMA['schema'],SCHEMA['mappings'],metrics.BASE)
            storage.save(response,processed,files)
            st.session_state['last_receipt']=response
            st.rerun()

def manager_html(journals):
    html=(ROOT/'assets/index.html').read_text(encoding='utf-8')
    css=(ROOT/'assets/style.css').read_text(encoding='utf-8')
    js=(ROOT/'assets/app.js').read_text(encoding='utf-8')
    dataset={'journals':journals,'fields':[{'id':f['field_id'],'label':f['label'],'section':f['section']} for f in FIELDS],'refreshed_at':datetime.now(timezone.utc).isoformat()}
    payload=json.dumps(dataset,ensure_ascii=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    html=html.replace('<link rel="stylesheet" href="/static/style.css">','<style>'+css+'</style>')
    html=html.replace('<script src="/static/app.js" defer></script>','<script type="application/json" id="portal-dataset">'+payload+'</script><script>'+js+'</script>')
    html=html.replace('Prévia local · dados reais','Portal de teste · base compartilhada').replace('Consultas em leitura à base do formulário.','Novos envios deste portal e cinco exemplos de demonstração.')
    html=html.replace('<a href="http://127.0.0.1:8200/" target="_blank" rel="noopener">Abrir formulário ↗</a>','')
    html=html.replace('Cinco','Cinco').replace('Protocolos na aplicação própria','Exemplos e envios deste portal')
    html=html.replace('Google Forms: 12 respostas observadas em 27/10/2026 · base histórica separada, sem sincronização nesta prévia.','Ambiente de teste. Exemplos locais não são sincronizados; novos envios deste portal aparecem ao atualizar o painel.')
    return html

def manager_page():
    left,right=st.columns([4,1])
    left.caption('Formulário e painel compartilham os envios deste portal. Os cinco exemplos iniciais são um recorte sem contatos e sem anexos originais.')
    if right.button('Atualizar submissões'):st.rerun()
    st.warning('Armazenamento temporário de teste. Exporte o backup após as conferências; reinícios do Streamlit podem apagar novos envios. Para uso contínuo, conectar armazenamento persistente.')
    st.download_button('Exportar backup dos novos envios e anexos',storage.export_backup(),file_name='educa-backup-teste.json',mime='application/json')
    with st.expander('Restaurar backup de um teste anterior'):
        backup=st.file_uploader('Backup JSON exportado pelo portal',type=['json'],key='restore_backup')
        if st.button('Restaurar registros do backup'):
            if not backup:st.error('Selecione o backup JSON.')
            else:
                try:count=storage.restore_backup(backup.getvalue());st.success(f'{count} registro(s) recuperados. Os registros atuais foram preservados.')
                except (ValueError,KeyError,TypeError):st.error('Backup inválido ou incompatível com este portal.')
    journals=[]
    for row in storage.rows():
        response=json.loads(row['response_json'])
        for field in metrics.FILES:
            file=storage.attachment(row['response_id'],field)
            if file and response.get(field):
                path=metrics.safe_file(response[field])
                if path is None:
                    path=metrics.BASE/response[field];path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(file['content'])
        journal=metrics.submission(row)
        for annex in journal['annexes']:
            file=storage.attachment(row['response_id'],annex['field'])
            annex['url']='data:application/octet-stream;base64,'+base64.b64encode(file['content']).decode() if file else None
            if file:annex['name']=file['name']
        journals.append(journal)
    if st.checkbox('Incluir as cinco revistas de demonstração',value=True):journals+=json.loads((ROOT/'data/demo_journals.json').read_text(encoding='utf-8'))
    st.iframe(manager_html(journals),height=1050,alt='Painel Educ@ com respostas, métricas e gráficos')

mode=st.query_params.get('area','formulario')
area=st.radio('Área do portal',['Formulário de teste','Área do gestor'],index=1 if mode=='gestor' else 0,horizontal=True)
st.query_params['area']='gestor' if area=='Área do gestor' else 'formulario'
if area=='Área do gestor':manager_page()
else:
    from form_portal import render_form
    st.markdown('<style>.block-container{max-width:none;padding:0!important}section[data-testid="stMain"]{padding:0}header[data-testid="stHeader"]{display:none}div[data-testid="stRadio"]{padding:8px 4vw;background:white;margin:0}div[data-testid="stCustomComponentV1"]{width:100%}</style>',unsafe_allow_html=True)
    render_form(pdf_receipt)
