import io
import json
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook
import metrics
import storage
from rules import preprocess_response, validate_response

ROOT=Path(__file__).resolve().parent

class PortalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.old=(storage.RUNTIME,storage.DATABASE,metrics.BASE)
        storage.RUNTIME=Path(self.temp.name)
        storage.DATABASE=storage.RUNTIME/'test.sqlite3'
        metrics.BASE=storage.RUNTIME

    def tearDown(self):
        storage.RUNTIME,storage.DATABASE,metrics.BASE=self.old
        self.temp.cleanup()

    def test_submission_is_shared_and_idempotent(self):
        response={'response_id':'TEST-1','journal_title':'Revista de teste','create_time':'2026-10-27T20:00:00+00:00','submission_year':2026,'published_articles_y1':10,'international_articles_y1':2}
        processed={'status':'Revisão necessária','warnings':[],'criteria':[]}
        storage.save(response,processed,{})
        storage.save(response,processed,{})
        rows=storage.rows()
        self.assertEqual(len(rows),1)
        item=metrics.submission(rows[0])
        self.assertEqual(item['title'],'Revista de teste')
        self.assertEqual(item['annual'][1]['international_pct'],20)

    def test_annex_and_backup_round_trip(self):
        response={'response_id':'TEST-2','journal_title':'Revista de teste','create_time':'2026-10-27T20:00:00+00:00','editorial_dates_file':'runtime/uploads/test.xlsx'}
        processed={'status':'Pré-processado'}
        storage.save(response,processed,{'editorial_dates_file':{'name':'artigos.xlsx','content':b'file-content'}})
        backup=storage.export_backup()
        storage.DATABASE=storage.RUNTIME/'restored.sqlite3'
        self.assertEqual(storage.restore_backup(backup),1)
        self.assertEqual(storage.attachment('TEST-2','editorial_dates_file')['content'],b'file-content')
        self.assertEqual(storage.restore_backup(backup),1)
        self.assertEqual(len(storage.rows()),1)

    def test_backup_rejects_unsafe_paths(self):
        response={'response_id':'TEST-3','journal_title':'Test','create_time':'2026-10-27T20:00:00+00:00','authors_file':'../../outside.xlsx'}
        invalid={'schema_version':1,'submissions':[{'response_id':'TEST-3','response_json':json.dumps(response),'processed_json':'{}'}]}
        with self.assertRaises(ValueError):storage.restore_backup(json.dumps(invalid).encode())

    def test_original_schema_and_required_conditions(self):
        schema=json.loads((ROOT/'data/schema.json').read_text(encoding='utf-8'))
        self.assertEqual(len(schema['fields']),98)
        self.assertEqual(len(schema['sections']),12)
        self.assertEqual(len(schema['mappings']),44)
        warnings,_=validate_response({'doi_adoption':'Sim'},schema['schema'],metrics.BASE)
        self.assertTrue(any('doi_start_year' in w['field_ids'] for w in warnings))

if __name__=='__main__':unittest.main()
