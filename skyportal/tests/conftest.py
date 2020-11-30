"""Test fixture configuration."""

import pytest
import os
import uuid
import pathlib
from datetime import datetime, timedelta
from pathlib import Path
import numpy as np

from baselayer.app import models
from baselayer.app.config import load_config
from baselayer.app.test_util import (  # noqa: F401
    driver,
    set_server_url,
)

from skyportal.tests.fixtures import TMP_DIR  # noqa: F401
from skyportal.tests.fixtures import (
    ObjFactory,
    StreamFactory,
    GroupFactory,
    UserFactory,
    FilterFactory,
    InstrumentFactory,
    ObservingRunFactory,
    TelescopeFactory,
    ClassicalAssignmentFactory,
)
from skyportal.model_util import create_token
from skyportal.models import (
    DBSession,
    Source,
    Candidate,
    Role,
    User,
    Allocation,
    FollowupRequest,
)

import astroplan


print("Loading test configuration from _test_config.yaml")
basedir = pathlib.Path(os.path.dirname(__file__))
cfg = load_config([(basedir / "../../test_config.yaml").absolute()])
set_server_url(f'http://localhost:{cfg["ports.app"]}')
print("Setting test database to:", cfg["database"])
models.init_db(**cfg["database"])

# Add a "test factory" User so that all factory-generated comments have a
# proper author, if it doesn't already exist (the user may already be in
# there if running the test server and running tests individually)
if not DBSession.query(User).filter(User.username == "test factory").scalar():
    DBSession.add(User(username="test factory"))
    DBSession.commit()


def pytest_runtest_setup(item):
    # Print timestamp when running each test
    print(datetime.now().strftime('[%H:%M:%S] '), end='')


# set up a hook to be able to check if a test has failed
@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    # execute all other hooks to obtain the report object

    outcome = yield
    rep = outcome.get_result()

    # set a report attribute for each phase of a call, which can
    # be "setup", "call", "teardown"

    setattr(item, "rep_" + rep.when, rep)


# check if a test has failed
@pytest.fixture(scope="function", autouse=True)
def test_failed_check(request):

    gecko = Path('geckodriver.log')
    gecko.touch(exist_ok=True)

    # get the number of bytes in the file currently
    log_bytes = os.path.getsize(gecko)

    # add a separator to the geckodriver logs
    with open(gecko, 'a') as f:
        f.write(f'BEGIN {request.node.nodeid}\n')

    yield
    # request.node is an "item" because we use the default
    # "function" scope

    # add a separator to the geckodriver logs
    with open(gecko, 'a') as f:
        f.write(f'END {request.node.nodeid}\n')

    if request.node.rep_call.failed and 'driver' in request.node.funcargs:
        webdriver = request.node.funcargs['driver']
        take_screenshot_and_page_source(webdriver, request.node.nodeid)

    # delete the interstitial data from the geckodriver log by
    # truncating the file back to its original number of bytes
    with open(gecko, 'a') as f:
        f.truncate(log_bytes)


# make a screenshot with a name of the test, date and time.
# also save the page HTML.
def take_screenshot_and_page_source(webdriver, nodeid):
    file_name = f'{nodeid}_{datetime.today().strftime("%Y-%m-%d_%H:%M")}.png'.replace(
        "/", "_"
    ).replace(":", "_")
    file_name = os.path.join(os.path.dirname(__file__), '../../test-results', file_name)
    Path(file_name).parent.mkdir(parents=True, exist_ok=True)

    webdriver.save_screenshot(file_name)
    with open(file_name.replace('png', 'html'), 'w') as f:
        f.write(webdriver.page_source)

    file_name = f'{nodeid}_{datetime.today().strftime("%Y-%m-%d_%H:%M")}.console.log'.replace(
        "/", "_"
    ).replace(
        ":", "_"
    )
    file_name = os.path.join(
        os.path.dirname(__file__), '../../webdriver-console', file_name
    )
    Path(file_name).parent.mkdir(parents=True, exist_ok=True)

    with open(file_name, 'w') as f, open('geckodriver.log', 'r') as gl:
        lines = gl.readlines()
        revlines = list(reversed(lines))
        istart = revlines.index(f'BEGIN {nodeid}\n')
        iend = revlines.index(f'END {nodeid}\n')
        f.write('\n'.join(list(reversed(revlines[iend : (istart + 1)]))))  # noqa: E203


@pytest.fixture()
def public_stream():
    return StreamFactory()


@pytest.fixture()
def public_stream2():
    return StreamFactory()


@pytest.fixture()
def stream_with_users(super_admin_user, group_admin_user, user, view_only_user):
    return StreamFactory(
        users=[super_admin_user, group_admin_user, user, view_only_user]
    )


@pytest.fixture()
def public_group():
    return GroupFactory()


@pytest.fixture()
def public_group2():
    return GroupFactory()


@pytest.fixture()
def group_with_stream(
    super_admin_user, group_admin_user, user, view_only_user, public_stream
):
    return GroupFactory(
        users=[super_admin_user, group_admin_user, user, view_only_user],
        streams=[public_stream],
    )


@pytest.fixture()
def group_with_stream_with_users(
    super_admin_user, group_admin_user, user, view_only_user, stream_with_users
):
    return GroupFactory(
        users=[super_admin_user, group_admin_user, user, view_only_user],
        streams=[stream_with_users],
    )


@pytest.fixture()
def public_filter(public_group, public_stream):
    return FilterFactory(group=public_group, stream=public_stream)


@pytest.fixture()
def public_filter2(public_group2, public_stream):
    return FilterFactory(group=public_group2, stream=public_stream)


@pytest.fixture()
def public_ZTF20acgrjqm(public_group):
    obj = ObjFactory(groups=[public_group], ra=65.0630767, dec=82.5880983)
    DBSession().add(Source(obj_id=obj.id, group_id=public_group.id))
    DBSession().commit()
    return obj


@pytest.fixture()
def public_source(public_group):
    obj = ObjFactory(groups=[public_group])
    DBSession.add(Source(obj_id=obj.id, group_id=public_group.id))
    DBSession.commit()
    return obj


@pytest.fixture()
def public_source_two_groups(public_group, public_group2):
    obj = ObjFactory(groups=[public_group, public_group2])
    for group in [public_group, public_group2]:
        DBSession.add(Source(obj_id=obj.id, group_id=group.id))
    DBSession.commit()
    return obj


@pytest.fixture()
def public_source_group2(public_group2):
    obj = ObjFactory(groups=[public_group2])
    DBSession.add(Source(obj_id=obj.id, group_id=public_group2.id))
    DBSession.commit()
    return obj


@pytest.fixture()
def public_candidate(public_filter, user):
    obj = ObjFactory(groups=[public_filter.group])
    DBSession.add(
        Candidate(
            obj=obj,
            filter=public_filter,
            passed_at=datetime.utcnow() - timedelta(seconds=np.random.randint(0, 100)),
            uploader_id=user.id,
        )
    )
    DBSession.commit()
    return obj


@pytest.fixture()
def public_candidate_two_groups(
    public_filter, public_filter2, public_group, public_group2, user
):
    obj = ObjFactory(groups=[public_group, public_group2])
    DBSession.add(
        Candidate(
            obj=obj,
            filter=public_filter,
            passed_at=datetime.utcnow() - timedelta(seconds=np.random.randint(0, 100)),
            uploader_id=user.id,
        )
    )
    DBSession.add(
        Candidate(
            obj=obj,
            filter=public_filter2,
            passed_at=datetime.utcnow() - timedelta(seconds=np.random.randint(0, 100)),
            uploader_id=user.id,
        )
    )
    DBSession.commit()
    return obj


@pytest.fixture()
def public_candidate2(public_filter, user):
    obj = ObjFactory(groups=[public_filter.group])
    DBSession.add(
        Candidate(
            obj=obj,
            filter=public_filter,
            passed_at=datetime.utcnow() - timedelta(seconds=np.random.randint(0, 100)),
            uploader_id=user.id,
        )
    )
    DBSession.commit()
    return obj


@pytest.fixture()
def public_obj(public_group):
    return ObjFactory(groups=[public_group])


@pytest.fixture()
def red_transients_group(group_admin_user, view_only_user):
    return GroupFactory(
        name=f'red transients-{uuid.uuid4().hex}',
        users=[group_admin_user, view_only_user],
    )


@pytest.fixture()
def ztf_camera():
    return InstrumentFactory()


@pytest.fixture()
def hst():
    return TelescopeFactory(
        name=f'Hubble Space Telescope_{uuid.uuid4()}',
        nickname=f'HST_{uuid.uuid4()}',
        lat=0,
        lon=0,
        elevation=0,
        diameter=2.0,
        fixed_location=False,
    )


@pytest.fixture()
def keck1_telescope():
    observer = astroplan.Observer.at_site('Keck')
    return TelescopeFactory(
        name=f'Keck I Telescope_{uuid.uuid4()}',
        nickname=f'Keck1_{uuid.uuid4()}',
        lat=observer.location.lat.to('deg').value,
        lon=observer.location.lon.to('deg').value,
        elevation=observer.location.height.to('m').value,
        diameter=10.0,
    )


@pytest.fixture()
def wise_18inch():
    return TelescopeFactory(
        name=f'Wise 18-inch Telescope_{uuid.uuid4()}',
        nickname=f'Wise18_{uuid.uuid4()}',
        lat=34.763333,
        lon=30.595833,
        elevation=875,
        diameter=0.46,
    )


@pytest.fixture()
def xinglong_216cm():
    return TelescopeFactory(
        name=f'Xinglong 2.16m_{uuid.uuid4()}',
        nickname='XL216_{uuid.uuid4()}',
        lat=40.004463,
        lon=116.385556,
        elevation=950.0,
        diameter=2.16,
    )


@pytest.fixture()
def p60_telescope():
    observer = astroplan.Observer.at_site('Palomar')
    return TelescopeFactory(
        name=f'Palomar 60-inch telescope_{uuid.uuid4()}',
        nickname='p60_{uuid.uuid4()}',
        lat=observer.location.lat.to('deg').value,
        lon=observer.location.lon.to('deg').value,
        elevation=observer.location.height.to('m').value,
        diameter=1.6,
    )


@pytest.fixture()
def lris(keck1_telescope):
    return InstrumentFactory(
        name=f'LRIS_{uuid.uuid4()}',
        type='imaging spectrograph',
        telescope=keck1_telescope,
        band='Optical',
        filters=[
            'sdssu',
            'sdssg',
            'sdssr',
            'sdssi',
            'sdssz',
            'bessellux',
            'bessellv',
            'bessellb',
            'bessellr',
            'besselli',
        ],
    )


@pytest.fixture()
def sedm(p60_telescope):
    return InstrumentFactory(
        name=f'SEDM_{uuid.uuid4()}',
        type='imaging spectrograph',
        telescope=p60_telescope,
        band='Optical',
        filters=['sdssu', 'sdssg', 'sdssr', 'sdssi'],
        api_classname='SEDMAPI',
        listener_classname='SEDMListener',
    )


@pytest.fixture()
def red_transients_run():
    return ObservingRunFactory()


@pytest.fixture()
def lris_run_20201118(lris, public_group, super_admin_user):
    return ObservingRunFactory(
        instrument=lris,
        group=public_group,
        calendar_date='2020-11-18',
        owner=super_admin_user,
    )


@pytest.fixture()
def problematic_assignment(lris_run_20201118, public_ZTF20acgrjqm):
    return ClassicalAssignmentFactory(
        run=lris_run_20201118,
        obj=public_ZTF20acgrjqm,
        requester=lris_run_20201118.owner,
        last_modified_by=lris_run_20201118.owner,
    )


@pytest.fixture()
def private_source():
    return ObjFactory(groups=[])


@pytest.fixture()
def user(public_group):
    return UserFactory(
        groups=[public_group], roles=[models.Role.query.get("Full user")]
    )


@pytest.fixture()
def user_group2(public_group2):
    return UserFactory(
        groups=[public_group2], roles=[models.Role.query.get("Full user")]
    )


@pytest.fixture()
def user2(public_group):
    return UserFactory(
        groups=[public_group], roles=[models.Role.query.get("Full user")]
    )


@pytest.fixture()
def user_no_groups():
    return UserFactory(roles=[models.Role.query.get("Full user")])


@pytest.fixture()
def user_two_groups(public_group, public_group2):
    return UserFactory(
        groups=[public_group, public_group2], roles=[models.Role.query.get("Full user")]
    )


@pytest.fixture()
def view_only_user(public_group):
    return UserFactory(
        groups=[public_group], roles=[models.Role.query.get("View only")]
    )


@pytest.fixture()
def view_only_user2(public_group):
    return UserFactory(
        groups=[public_group], roles=[models.Role.query.get("View only")]
    )


@pytest.fixture()
def group_admin_user(public_group):
    return UserFactory(
        groups=[public_group], roles=[models.Role.query.get("Group admin")]
    )


@pytest.fixture()
def group_admin_user_two_groups(public_group, public_group2):
    return UserFactory(
        groups=[public_group, public_group2],
        roles=[models.Role.query.get("Group admin")],
    )


@pytest.fixture()
def super_admin_user(public_group):
    return UserFactory(
        groups=[public_group], roles=[models.Role.query.get("Super admin")]
    )


@pytest.fixture()
def super_admin_user_two_groups(public_group, public_group2):
    return UserFactory(
        groups=[public_group, public_group2],
        roles=[models.Role.query.get("Super admin")],
    )


@pytest.fixture()
def view_only_token(user):
    token_id = create_token(ACLs=[], user_id=user.id, name=str(uuid.uuid4()))
    return token_id


@pytest.fixture()
def view_only_token2(user2):
    token_id = create_token(ACLs=[], user_id=user2.id, name=str(uuid.uuid4()))
    return token_id


@pytest.fixture()
def view_only_token_group2(user_group2):
    token_id = create_token(ACLs=[], user_id=user_group2.id, name=str(uuid.uuid4()))
    return token_id


@pytest.fixture()
def view_only_token_two_groups(user_two_groups):
    token_id = create_token(ACLs=[], user_id=user_two_groups.id, name=str(uuid.uuid4()))
    return token_id


@pytest.fixture()
def manage_sources_token(group_admin_user):
    token_id = create_token(
        ACLs=["Manage sources"], user_id=group_admin_user.id, name=str(uuid.uuid4()),
    )
    return token_id


@pytest.fixture()
def manage_sources_token_two_groups(group_admin_user_two_groups):
    token_id = create_token(
        ACLs=["Manage sources"],
        user_id=group_admin_user_two_groups.id,
        name=str(uuid.uuid4()),
    )
    return token_id


@pytest.fixture()
def upload_data_token(user):
    token_id = create_token(
        ACLs=["Upload data"], user_id=user.id, name=str(uuid.uuid4())
    )
    return token_id


@pytest.fixture()
def upload_data_token_two_groups(user_two_groups):
    token_id = create_token(
        ACLs=["Upload data"], user_id=user_two_groups.id, name=str(uuid.uuid4()),
    )
    return token_id


@pytest.fixture()
def manage_groups_token(super_admin_user):
    token_id = create_token(
        ACLs=["Manage groups"], user_id=super_admin_user.id, name=str(uuid.uuid4()),
    )
    return token_id


@pytest.fixture()
def manage_users_token(super_admin_user):
    token_id = create_token(
        ACLs=["Manage users"], user_id=super_admin_user.id, name=str(uuid.uuid4()),
    )
    return token_id


@pytest.fixture()
def super_admin_token(super_admin_user):
    role = Role.query.get("Super admin")
    token_id = create_token(
        ACLs=[a.id for a in role.acls],
        user_id=super_admin_user.id,
        name=str(uuid.uuid4()),
    )
    return token_id


@pytest.fixture()
def super_admin_token_two_groups(super_admin_user_two_groups):
    role = Role.query.get("Super admin")
    token_id = create_token(
        ACLs=[a.id for a in role.acls],
        user_id=super_admin_user_two_groups.id,
        name=str(uuid.uuid4()),
    )
    return token_id


@pytest.fixture()
def comment_token(user):
    token_id = create_token(ACLs=["Comment"], user_id=user.id, name=str(uuid.uuid4()))
    return token_id


@pytest.fixture()
def annotation_token(user):
    token_id = create_token(ACLs=["Annotate"], user_id=user.id, name=str(uuid.uuid4()))
    return token_id


@pytest.fixture()
def classification_token(user):
    token_id = create_token(ACLs=["Classify"], user_id=user.id, name=str(uuid.uuid4()))
    return token_id


@pytest.fixture()
def classification_token_two_groups(user_two_groups):
    token_id = create_token(
        ACLs=["Classify"], user_id=user_two_groups.id, name=str(uuid.uuid4())
    )
    return token_id


@pytest.fixture()
def taxonomy_token(user):
    token_id = create_token(
        ACLs=["Post taxonomy", "Delete taxonomy"],
        user_id=user.id,
        name=str(uuid.uuid4()),
    )
    return token_id


@pytest.fixture()
def taxonomy_token_two_groups(user_two_groups):
    token_id = create_token(
        ACLs=["Post taxonomy", "Delete taxonomy"],
        user_id=user_two_groups.id,
        name=str(uuid.uuid4()),
    )
    return token_id


@pytest.fixture()
def comment_token_two_groups(user_two_groups):
    token_id = create_token(
        ACLs=["Comment"], user_id=user_two_groups.id, name=str(uuid.uuid4())
    )
    return token_id


@pytest.fixture()
def annotation_token_two_groups(user_two_groups):
    token_id = create_token(
        ACLs=["Annotate"], user_id=user_two_groups.id, name=str(uuid.uuid4())
    )
    return token_id


@pytest.fixture()
def public_group_sedm_allocation(sedm, public_group):
    allocation = Allocation(
        instrument=sedm,
        group=public_group,
        pi=str(uuid.uuid4()),
        proposal_id=str(uuid.uuid4()),
        hours_allocated=100,
    )
    DBSession().add(allocation)
    DBSession().commit()
    return allocation


@pytest.fixture()
def public_group2_sedm_allocation(sedm, public_group2):
    allocation = Allocation(
        instrument=sedm,
        group=public_group2,
        pi=str(uuid.uuid4()),
        proposal_id=str(uuid.uuid4()),
        hours_allocated=100,
    )
    DBSession().add(allocation)
    DBSession().commit()
    return allocation


@pytest.fixture()
def public_source_followup_request(public_group_sedm_allocation, public_source, user):
    fr = FollowupRequest(
        obj=public_source,
        allocation=public_group_sedm_allocation,
        payload={
            'priority': "5",
            'start_date': '3020-09-01',
            'end_date': '3022-09-01',
            'observation_type': 'IFU',
        },
        requester_id=user.id,
        last_modified_by_id=user.id,
    )

    DBSession().add(fr)
    DBSession().commit()
    return fr


@pytest.fixture()
def public_source_group2_followup_request(
    public_group2_sedm_allocation, public_source_group2, user_two_groups
):
    fr = FollowupRequest(
        obj=public_source_group2,
        allocation=public_group2_sedm_allocation,
        payload={
            'priority': "5",
            'start_date': '3020-09-01',
            'end_date': '3022-09-01',
            'observation_type': 'IFU',
        },
        requester_id=user_two_groups.id,
        last_modified_by_id=user_two_groups.id,
    )

    DBSession().add(fr)
    DBSession().commit()
    return fr


@pytest.fixture()
def sedm_listener_token(sedm, group_admin_user):
    token_id = create_token(
        ACLs=[sedm.listener_class.get_acl_id()],
        user_id=group_admin_user.id,
        name=str(uuid.uuid4()),
    )
    return token_id


@pytest.fixture()
def source_notification_user(public_group):
    uid = str(uuid.uuid4())
    username = f"{uid}@cesium.ml.org"
    user = User(
        username=username,
        contact_email=username,
        contact_phone="+12345678910",
        groups=[public_group],
        roles=[models.Role.query.get("Full user")],
        preferences={"allowEmailNotifications": True, "allowSMSNotifications": True},
    )
    DBSession().add(user)
    DBSession().commit()
    return user


@pytest.fixture()
def source_notification_user_token(source_notification_user):
    token_id = create_token(
        ACLs=[], user_id=source_notification_user.id, name=str(uuid.uuid4()),
    )
    return token_id
