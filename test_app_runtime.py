"""Verifica navegação e envio completo sem fontes externas nem banco original."""
import json
from pathlib import Path
import tempfile
import unittest
from streamlit.testing.v1 import AppTest
import metrics
import storage

ROOT=Path(__file__).resolve().parent

class RuntimeTests(unittest.TestCase):
    def test_steps_preserve_draft(self):
        app=AppTest.from_file(ROOT/'streamlit_app.py',default_timeout=30).run()
        app.text_input(key='value_journal_title').set_value('Revista fictícia de navegação').run()
        for index in range(1,12):
            app.selectbox(key='form_section').set_value(index).run()
            self.assertFalse(app.exception)
        app.selectbox(key='form_section').set_value(0).run()
        self.assertEqual(app.text_input(key='value_journal_title').value,'Revista fictícia de navegação')

    def test_submit_receipt_and_shared_manager_record(self):
        schema=json.loads((ROOT/'data/schema.json').read_text(encoding='utf-8'))
        response={}
        for f in schema['fields']:
            key=f['field_id'];kind=f['type']
            if f['required']!='Sim' or f['section']=='Controle':continue
            if kind=='sim_nao':value='Não'
            elif kind=='lista':value=f['options'][0]
            elif kind=='inteiro':value=2026 if key=='submission_year' else 2000 if 'year' in key else 6
            elif kind=='arquivo':continue
            elif kind=='url':value='https://example.org/revista'
            elif kind=='email':value='teste@example.org'
            elif kind=='multipla_escolha':value='Português, Inglês'
            else:value='Informação fictícia de teste'
            response[key]=value
        response.update(journal_title='Acta Scientiarum. Education',issn_online='2178-5201',issn_print='2178-5198',main_email='teste@example.org',country='Brasil',state_region='Paraná',city='Maringá',periodicity='Anual')
        files={field:{'name':name,'content':(ROOT/'templates'/name).read_bytes()} for field,name in {'reviewers_file':'Modelo_Pareceristas.xlsx','editorial_dates_file':'Modelo_Datas_Artigos.xlsx','editorial_board_file':'Modelo_Conselho_Editorial.xlsx','authors_file':'Modelo_Autores_Artigos.xlsx'}.items()}
        old=(storage.RUNTIME,storage.DATABASE,metrics.BASE)
        try:
            with tempfile.TemporaryDirectory() as folder:
                storage.RUNTIME=Path(folder);storage.DATABASE=Path(folder)/'test.sqlite3';metrics.BASE=Path(folder)
                app=AppTest.from_file(ROOT/'streamlit_app.py',default_timeout=30)
                app.session_state['draft_values']=response
                app.session_state['draft_files']=files
                app.session_state['identity']={'confirmed':True,'title':'Acta Scientiarum. Education'}
                app.session_state['identity_issn']='2178-5201'
                app.run()
                app.selectbox(key='form_section').set_value(11).run()
                app.checkbox(key='test_consent').check().run()
                next(b for b in app.button if b.label=='Enviar submissão de teste').click().run()
                self.assertFalse(app.exception)
                self.assertFalse(app.error,[error.value for error in app.error])
                self.assertEqual(len(storage.rows()),1)
                self.assertIn('last_receipt',app.session_state)
                item=metrics.submission(storage.rows()[0])
                self.assertEqual(len([a for a in item['annexes'] if a['exists']]),4)
        finally:
            storage.RUNTIME,storage.DATABASE,metrics.BASE=old

if __name__=='__main__':unittest.main()
