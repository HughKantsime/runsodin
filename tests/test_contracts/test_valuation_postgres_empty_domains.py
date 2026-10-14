"""Empty alias domains must compile with the compared PostgreSQL column type."""
import pytest
from sqlalchemy.dialects import postgresql
from modules.archives.valuation_content_visibility import _bounded


class Capture:
    def execute(self,query,params):
        self.sql=str(query.params(**params).compile(dialect=postgresql.dialect(),compile_kwargs={'literal_binds':True}))
        return self
    def mappings(self):return self
    def all(self):return []


@pytest.mark.parametrize('domain,column,cast',[('paths','stored_path','VARCHAR'),('operations','operation_id','VARCHAR'),('files','id','INTEGER'),('models','model_id','INTEGER')])
def test_empty_domains_retain_postgres_comparison_types(domain,column,cast):
    db=Capture()
    _bounded(db,f'SELECT id FROM owned_aliases WHERE {column} IN :{domain}',{domain:[]},(domain,))
    assert f'CAST(NULL AS {cast})' in db.sql
    assert 'LIMIT 17' in db.sql


def test_unknown_domain_cannot_choose_an_inferred_type():
    with pytest.raises(ValueError,match='Unknown content alias expansion domain'):
        _bounded(Capture(),'SELECT id WHERE id IN :unapproved',{'unapproved':[]},('unapproved',))
