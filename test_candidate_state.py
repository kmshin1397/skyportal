from baselayer.app.env import load_env
from skyportal.models import DBSession, Candidate

env, cfg = load_env()

print(DBSession().query(Candidate.id, Candidate.passed_at).all())
