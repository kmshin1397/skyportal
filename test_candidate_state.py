from baselayer.app.env import load_env
from baselayer.app.models import init_db
from skyportal.models import DBSession, Candidate

env, cfg = load_env()
init_db(**cfg["database"])
print(DBSession().query(Candidate.id, Candidate.passed_at).all())
