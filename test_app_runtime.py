import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import form_portal
import metrics
import storage

ROOT=Path(__file__).resolve().parent

class OriginalFormTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.old=(metrics.BASE,storage.RUNTIME,storage.DATABASE)
        metrics.BASE=Path(self.temp.name)
        storage.RUNTIME=metrics.BASE
        storage.DATABASE=metrics.BASE/'portal.sqlite3'
        self.session={}
    def tearDown(self):
        metrics.BASE,storage.RUNTIME,storage.DATABASE=self.old
        self.temp.cleanup()
    def call(self,url,body=None):
        return form_portal.dispatch({'url':url,'body':body or {}},self.session,lambda r:b'%PDF-test')
    def test_draft_and_ui_state_roundtrip(self):
        body={'draft_id':'test','response':{'journal_title':'Revista de teste'},'current_section':4,'unlocked_section':4}
        self.call('/api/drafts',body)
        draft=self.call('/api/drafts/test')
        self.assertEqual(draft['response']['journal_title'],'Revista de teste')
        self.assertEqual(draft['ui_state']['current_section'],4)
    def test_original_templates_and_layout_are_included(self):
        html=(ROOT/'form_component/index.html').read_text(encoding='utf-8')
        self.assertIn('class="topbar"',html)
        self.assertIn('class="review-panel"',html)
        self.assertIn('id="section-nav"',html)
        self.assertEqual(len(list((ROOT/'form_component/templates').glob('*.xlsx'))),4)
    def test_attachment_is_scoped_to_the_session(self):
        result=self.call('/api/uploads',{'filename':'dados.csv','content_b64':base64.b64encode(b'ano,titulo_artigo\n2025,Teste').decode()})
        response={'editorial_dates_file':result['declared_path']}
        self.assertEqual(form_portal.checked_response(response,self.session['uploads'])['editorial_dates_file'],result['declared_path'])
        with self.assertRaises(ValueError):form_portal.checked_response(response,{})
    def test_section_validation_blocks_missing_required_fields(self):
        result=self.call('/api/validate-section',{'section':'Dados de identificação','response':{}})
        self.assertFalse(result['can_continue'])
        self.assertTrue(result['warnings'])
    def test_completed_submission_is_idempotent(self):
        original=json.loads((ROOT/'data/demo_journals.json').read_text(encoding='utf-8'))[0]['response']
        original.update(main_email='teste@example.org',respondent_email='teste@example.org',editor_name='Editor de teste',phone='00000000',postal_address='Endereço de teste')
        self.call('/api/drafts',{'draft_id':'submit-test','response':original})
        for field in metrics.FILES:
            result=self.call('/api/uploads',{'filename':field+'.csv','content_b64':base64.b64encode(b'ano,titulo_artigo\n2025,Teste').decode()})
            original[field]=result['declared_path']
        identity={'verification':{'registry_confirmed':True},'errors':[]}
        with patch.object(form_portal,'validate_identity',return_value=identity),patch.object(form_portal,'validate_response',return_value=([],[])):
            body={'draft_id':'submit-test','response':original}
            first=self.call('/api/submissions',body);second=self.call('/api/submissions',body)
        self.assertEqual(first['receipt']['receipt_id'],second['receipt']['receipt_id'])
        self.assertEqual(len(storage.rows()),1)
        self.assertEqual(len([a for a in metrics.submission(storage.rows()[0])['annexes'] if a['exists']]),4)

if __name__=='__main__':unittest.main()
