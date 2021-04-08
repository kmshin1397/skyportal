from baselayer.app.env import load_env
from baselayer.app.models import init_db
from skyportal.models import DBSession, Candidate

env, cfg = load_env()
cfg["database"]["database"] = "skyportal_test"
init_db(**cfg["database"])
print(DBSession().query(Candidate.obj_id, Candidate.passed_at).all())
