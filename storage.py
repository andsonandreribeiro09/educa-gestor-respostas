"""Banco compartilhado do portal de teste; payloads e anexos na mesma transação."""
import base64
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / 'runtime'
DATABASE = RUNTIME / 'portal.sqlite3'

@contextmanager
def connect():
    RUNTIME.mkdir(exist_ok=True)
    connection = sqlite3.connect(DATABASE, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('PRAGMA busy_timeout=30000')
    connection.execute('CREATE TABLE IF NOT EXISTS submissions (response_id TEXT PRIMARY KEY,journal_title TEXT NOT NULL,status TEXT NOT NULL,response_json TEXT NOT NULL,processed_json TEXT NOT NULL,created_at TEXT NOT NULL)')
    connection.execute('CREATE TABLE IF NOT EXISTS attachments (response_id TEXT NOT NULL,field_id TEXT NOT NULL,name TEXT NOT NULL,content BLOB NOT NULL,PRIMARY KEY(response_id,field_id),FOREIGN KEY(response_id) REFERENCES submissions(response_id))')
    try:
        with connection:yield connection
    finally:connection.close()

def save(response, processed, attachments):
    with connect() as c:
        c.execute('INSERT OR IGNORE INTO submissions VALUES (?,?,?,?,?,?)',(response['response_id'],response['journal_title'],processed['status'],json.dumps(response,ensure_ascii=False),json.dumps(processed,ensure_ascii=False),response['create_time']))
        for field, attachment in attachments.items():
            c.execute('INSERT OR IGNORE INTO attachments VALUES (?,?,?,?)',(response['response_id'],field,attachment['name'],attachment['content']))

def rows():
    with connect() as c:
        return [dict(r) for r in c.execute('SELECT * FROM submissions ORDER BY created_at DESC')]

def attachment(response_id, field):
    with connect() as c:
        row = c.execute('SELECT name,content FROM attachments WHERE response_id=? AND field_id=?',(response_id,field)).fetchone()
        return dict(row) if row else None

def export_backup():
    with connect() as c:
        responses = [dict(r) for r in c.execute('SELECT * FROM submissions')]
        files = [{'response_id':r['response_id'],'field_id':r['field_id'],'name':r['name'],'content_b64':base64.b64encode(r['content']).decode()} for r in c.execute('SELECT * FROM attachments')]
    return json.dumps({'schema_version':1,'submissions':responses,'attachments':files},ensure_ascii=False).encode('utf-8')

def restore_backup(content):
    backup=json.loads(content)
    if backup.get('schema_version')!=1 or not isinstance(backup.get('submissions'),list):raise ValueError('Formato inválido')
    prepared=[]
    for row in backup['submissions']:
        response=json.loads(row['response_json']);processed=json.loads(row['processed_json'])
        if row['response_id']!=response['response_id']:raise ValueError('ID inconsistente')
        for key,value in response.items():
            if key.endswith('_file') and value:
                path=Path(str(value).replace('\\','/'))
                if path.is_absolute() or '..' in path.parts or not str(path).replace('\\','/').startswith('runtime/uploads/'):raise ValueError('Caminho inválido')
        files={}
        for file in backup.get('attachments',[]):
            if file['response_id']!=row['response_id']:continue
            data=base64.b64decode(file['content_b64'],validate=True)
            if len(data)>10*1024*1024:raise ValueError('Anexo muito grande')
            files[file['field_id']]={'name':Path(file['name']).name,'content':data}
        prepared.append((response,processed,files))
    for response,processed,files in prepared:save(response,processed,files)
    return len(prepared)
