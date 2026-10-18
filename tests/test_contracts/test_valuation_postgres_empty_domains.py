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
    # _bounded runs only its allowlisted alias statements; use the one that
    # compares this column with this empty domain.
    from modules.archives import valuation_content_visibility as module
    statement=next(s for s in sorted(module._BOUNDED_STATEMENTS) if f' {column} IN :{domain}' in s)
    domains=tuple(d for d in module._EXPANSION_TYPES if f':{d}' in statement)
    db=Capture()
    _bounded(db,statement,{d:[] for d in domains}|{'job':None},domains)
    assert f' {column} IN (SELECT CAST(NULL AS {cast}) WHERE 1!=1)' in db.sql
    assert 'LIMIT 17' in db.sql


def test_unknown_domain_cannot_choose_an_inferred_type():
    with pytest.raises(ValueError,match='Unknown content alias expansion domain'):
        _bounded(Capture(),'SELECT id WHERE id IN :unapproved',{'unapproved':[]},('unapproved',))
