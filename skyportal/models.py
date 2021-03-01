import json
import re
import uuid
import warnings
from datetime import datetime, timezone, timedelta

import arrow
import astroplan
import healpix_alchemy as ha
import numpy as np
import requests
import sqlalchemy as sa
import timezonefinder
import yaml
from astropy import coordinates as ap_coord
from astropy import time as ap_time
from astropy import units as u
from astropy.io import fits, ascii
from astropy.utils.exceptions import AstropyWarning
from slugify import slugify
from sqlalchemy import cast, event
from sqlalchemy.dialects import postgresql as psql
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import relationship, joinedload
from sqlalchemy.schema import UniqueConstraint
from sqlalchemy_utils import URLType, EmailType
from sqlalchemy_utils.types import JSONType
from sqlalchemy_utils.types.encrypted.encrypted_type import EncryptedType, AesEngine
from sqlalchemy import func

from twilio.rest import Client as TwilioClient

from baselayer.app.custom_exceptions import AccessError
from baselayer.app.env import load_env
from baselayer.app.json_util import to_json
from baselayer.app.models import (  # noqa
    init_db,
    join_model,
    Base,
    DBSession,
    ACL,
    Role,
    User,
    Token,
    UserACL,
    UserRole,
    UserAccessControl,
    AccessibleIfUserMatches,
    accessible_by_owner,
    restricted,
    public,
    AccessibleIfRelatedRowsAreAccessible,
    CronJobRun,
)
from skyportal import facility_apis
from . import schema
from .email_utils import send_email
from .enum_types import (
    allowed_bandpasses,
    thumbnail_types,
    instrument_types,
    followup_priorities,
    api_classnames,
    listener_classnames,
)
from .utils.cosmology import establish_cosmology

# In the AB system, a brightness of 23.9 mag corresponds to 1 microJy.
# All DB fluxes are stored in microJy (AB).
PHOT_ZP = 23.9
PHOT_SYS = 'ab'

utcnow = func.timezone('UTC', func.current_timestamp())

_, cfg = load_env()
cosmo = establish_cosmology(cfg)

# The minimum signal-to-noise ratio to consider a photometry point as a detection
PHOT_DETECTION_THRESHOLD = cfg["misc.photometry_detection_threshold_nsigma"]


def get_app_base_url():
    ports_to_ignore = {True: 443, False: 80}  # True/False <-> server.ssl=True/False
    return f"{'https' if cfg['server.ssl'] else 'http'}:" f"//{cfg['server.host']}" + (
        f":{cfg['server.port']}"
        if (
            cfg["server.port"] is not None
            and cfg["server.port"] != ports_to_ignore[cfg["server.ssl"]]
        )
        else ""
    )


def basic_user_display_info(user):
    return {
        field: getattr(user, field)
        for field in ('username', 'first_name', 'last_name', 'gravatar_url')
    }


def user_to_dict(self):
    return {
        field: getattr(self, field)
        for field in User.__table__.columns.keys()
        if field != "preferences"
    }


User.to_dict = user_to_dict


def is_readable_by(self, user_or_token):
    """Generic ownership logic for any `skyportal` ORM model.

    Models with complicated ownership logic should implement their own method
    instead of adding too many additional conditions here.
    """
    if hasattr(self, 'tokens'):
        return user_or_token in self.tokens
    if hasattr(self, 'groups'):
        return bool(set(self.groups) & set(user_or_token.accessible_groups))
    if hasattr(self, 'group'):
        return self.group in user_or_token.accessible_groups
    if hasattr(self, 'users'):
        if hasattr(user_or_token, 'created_by'):
            if user_or_token.created_by in self.users:
                return True
        return user_or_token in self.users

    raise NotImplementedError(f"{type(self).__name__} object has no owner")


def is_modifiable_by(self, user):
    """Return a boolean indicating whether an object point can be modified or
    deleted by a given user.

    Parameters
    ----------
    user: `baselayer.app.models.User`
       The User to check.

    Returns
    -------
    readable: bool
       Whether the Object can be modified by the User.
    """

    if not hasattr(self, 'owner'):
        raise TypeError(
            f'Object {self} does not have an `owner` attribute, '
            f'and thus does not expose the interface that is needed '
            f'to check for modification or deletion privileges.'
        )

    is_admin = "System admin" in user.permissions
    owns_spectrum = self.owner is user
    return is_admin or owns_spectrum


Base.is_readable_by = is_readable_by

accessible_by_groups_members = AccessibleIfUserMatches('groups.users')
accessible_by_group_members = AccessibleIfUserMatches('group.users')
accessible_by_members = AccessibleIfUserMatches('users')


class AccessibleIfGroupUserIsAdminAndUserMatches(AccessibleIfUserMatches):
    def __init__(self, relationship_chain):
        """A class that grants access to users related to a specific record
        through a chain of relationships. The relationship chain must
        contain a relationship called `group_users` and matches are only
        valid if the `admin` property of the corresponding `group_users` rows
        are true.

        Parameters
        ----------
        relationship_chain: str
            The chain of relationships to check the User or Token against in
            `query_accessible_rows`. Should be specified as

            >>>> f'{relationship1_name}.{relationship2_name}...{relationshipN_name}'

            The first relationship should be defined on the target class, and
            each subsequent relationship should be defined on the class pointed
            to by the previous relationship. If the querying user matches any
            record pointed to by the final relationship, the logic will grant
            access to the querying user.

        Examples
        --------

        Grant access if the querying user is an admin of any of the record's
        groups:

            >>>> AccessibleIfGroupUserIsAdminAndUserMatches('groups.group_users.user')

        Grant access if the querying user is an admin of the record's group:

            >>>> AccessibleIfUserMatches('group.group_users.user')
        """
        self.relationship_chain = relationship_chain

    @property
    def relationship_key(self):
        return self._relationship_key

    @relationship_key.setter
    def relationship_chain(self, value):
        if not isinstance(value, str):
            raise ValueError(
                f'Invalid value for relationship key: {value}, expected str, got {value.__class__.__name__}'
            )
        relationship_names = value.split('.')
        if 'group_users' not in value:
            raise ValueError('Relationship chain must contain "group_users".')
        if len(relationship_names) < 1:
            raise ValueError('Need at least 1 relationship to join on.')
        self._relationship_key = value

    def query_accessible_rows(self, cls, user_or_token, columns=None):
        """Construct a Query object that, when executed, returns the rows of a
        specified table that are accessible to a specified user or token.

        Parameters
        ----------
        cls: `baselayer.app.models.DeclarativeMeta`
            The mapped class of the target table.
        user_or_token: `baselayer.app.models.User` or `baselayer.app.models.Token`
            The User or Token to check.
        columns: list of sqlalchemy.Column, optional, default None
            The columns to retrieve from the target table. If None, queries
            the mapped class directly and returns mapped instances.

        Returns
        -------
        query: sqlalchemy.Query
            Query for the accessible rows.
        """

        query = super().query_accessible_rows(cls, user_or_token, columns=columns)
        if not user_or_token.is_admin:
            # this avoids name collisions
            group_user_subq = (
                DBSession()
                .query(GroupUser)
                .filter(GroupUser.admin.is_(True))
                .subquery()
            )
            query = query.join(
                group_user_subq,
                sa.and_(
                    Group.id == group_user_subq.c.group_id,
                    User.id == group_user_subq.c.user_id,
                ),
            )
        return query


accessible_by_group_admins = AccessibleIfGroupUserIsAdminAndUserMatches(
    'group.group_users.user'
)
accessible_by_admins = AccessibleIfGroupUserIsAdminAndUserMatches('group_users.user')


class NumpyArray(sa.types.TypeDecorator):
    """SQLAlchemy representation of a NumPy array."""

    impl = psql.ARRAY(sa.Float)

    def process_result_value(self, value, dialect):
        return np.array(value)


class Group(Base):
    """A user group. `Group`s controls `User` access to `Filter`s and serve as
    targets for data sharing requests. `Photometry` and `Spectra` shared with
    a `Group` will be visible to all its members. `Group`s maintain specific
    `Stream` permissions. In order for a `User` to join a `Group`, the `User`
    must have access to all of the `Group`'s data `Stream`s.
    """

    update = delete = accessible_by_admins
    member = accessible_by_members

    name = sa.Column(
        sa.String, unique=True, nullable=False, index=True, doc='Name of the group.'
    )
    nickname = sa.Column(
        sa.String, unique=True, nullable=True, index=True, doc='Short group nickname.'
    )
    private = sa.Column(
        sa.Boolean,
        nullable=False,
        default=False,
        index=True,
        doc="Boolean indicating whether group is invisible to non-members.",
    )
    streams = relationship(
        'Stream',
        secondary='group_streams',
        back_populates='groups',
        passive_deletes=True,
        doc='Stream access required for a User to become a member of the Group.',
    )
    filters = relationship(
        "Filter",
        back_populates="group",
        passive_deletes=True,
        doc='All filters (not just active) associated with a group.',
    )

    users = relationship(
        'User',
        secondary='group_users',
        back_populates='groups',
        passive_deletes=True,
        doc='The members of this group.',
    )

    group_users = relationship(
        'GroupUser',
        back_populates='group',
        cascade='save-update, merge, refresh-expire, expunge',
        passive_deletes=True,
        doc='Elements of a join table mapping Users to Groups.',
    )

    observing_runs = relationship(
        'ObservingRun',
        back_populates='group',
        doc='The observing runs associated with this group.',
    )
    photometry = relationship(
        "Photometry",
        secondary="group_photometry",
        back_populates="groups",
        cascade="save-update, merge, refresh-expire, expunge",
        passive_deletes=True,
        doc='The photometry visible to this group.',
    )

    spectra = relationship(
        "Spectrum",
        secondary="group_spectra",
        back_populates="groups",
        cascade="save-update, merge, refresh-expire, expunge",
        passive_deletes=True,
        doc='The spectra visible to this group.',
    )
    single_user_group = sa.Column(
        sa.Boolean,
        default=False,
        index=True,
        doc='Flag indicating whether this group '
        'is a singleton group for one user only.',
    )
    allocations = relationship(
        'Allocation',
        back_populates="group",
        cascade="save-update, merge, refresh-expire, expunge",
        passive_deletes=True,
        doc="Allocations made to this group.",
    )
    admission_requests = relationship(
        "GroupAdmissionRequest",
        back_populates="group",
        passive_deletes=True,
        doc="User requests to join this group.",
    )


GroupUser = join_model('group_users', Group, User)
GroupUser.__doc__ = "Join table mapping `Group`s to `User`s."
GroupUser.create = GroupUser.update = GroupUser.delete = (
    accessible_by_group_admins & GroupUser.read
)

GroupUser.admin = sa.Column(
    sa.Boolean,
    nullable=False,
    default=False,
    doc="Boolean flag indicating whether the User is an admin of the group.",
)

User.group_admission_requests = relationship(
    "GroupAdmissionRequest",
    back_populates="user",
    passive_deletes=True,
    doc="User's requests to join groups.",
)


class GroupAdmissionRequest(Base):
    """Table tracking requests from users to join groups."""

    user_id = sa.Column(
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="ID of the User requesting to join the group",
    )
    user = relationship(
        "User",
        foreign_keys=[user_id],
        back_populates="group_admission_requests",
        doc="The User requesting to join a group",
    )
    group_id = sa.Column(
        sa.ForeignKey("groups.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="ID of the Group to which admission is requested",
    )
    group = relationship(
        "Group",
        foreign_keys=[group_id],
        back_populates="admission_requests",
        doc="The Group to which admission is requested",
    )
    status = sa.Column(
        sa.Enum(
            "pending",
            "accepted",
            "declined",
            name="admission_request_status",
            validate_strings=True,
        ),
        nullable=False,
        default="pending",
        doc=(
            "Admission request status. Can be one of either 'pending', "
            "'accepted', or 'declined'."
        ),
    )


class Stream(Base):
    """A data stream producing alerts that can be programmatically filtered
    using a Filter."""

    read = AccessibleIfUserMatches('users')
    create = update = delete = restricted

    name = sa.Column(sa.String, unique=True, nullable=False, doc="Stream name.")
    altdata = sa.Column(
        JSONB,
        nullable=True,
        doc="Misc. metadata stored in JSON format, e.g. "
        "`{'collection': 'ZTF_alerts', selector: [1, 2]}`",
    )

    groups = relationship(
        'Group',
        secondary='group_streams',
        back_populates='streams',
        passive_deletes=True,
        doc="The Groups with access to this Stream.",
    )
    users = relationship(
        'User',
        secondary='stream_users',
        back_populates='streams',
        passive_deletes=True,
        doc="The users with access to this stream.",
    )
    filters = relationship(
        'Filter',
        back_populates='stream',
        passive_deletes=True,
        doc="The filters with access to this stream.",
    )


GroupStream = join_model('group_streams', Group, Stream)
GroupStream.__doc__ = "Join table mapping Groups to Streams."
GroupStream.create = GroupStream.update = GroupStream.delete = (
    accessible_by_group_admins & GroupStream.read
)


StreamUser = join_model('stream_users', Stream, User)
StreamUser.__doc__ = "Join table mapping Streams to Users."
StreamUser.create = restricted

User.groups = relationship(
    'Group',
    secondary='group_users',
    back_populates='users',
    passive_deletes=True,
    doc="The Groups this User is a member of.",
)

User.streams = relationship(
    'Stream',
    secondary='stream_users',
    back_populates='users',
    passive_deletes=True,
    doc="The Streams this User has access to.",
)


User.single_user_group = property(
    lambda self: DBSession()
    .query(Group)
    .join(GroupUser)
    .filter(Group.single_user_group.is_(True), GroupUser.user_id == self.id)
    .first()
)


@property
def user_or_token_accessible_groups(self):
    """Return the list of Groups a User or Token has access to. For non-admin
    Users or Token owners, this corresponds to the Groups they are a member of.
    For System Admins, this corresponds to all Groups."""
    if "System admin" in self.permissions:
        return Group.query.all()
    return self.groups


User.accessible_groups = user_or_token_accessible_groups
Token.accessible_groups = user_or_token_accessible_groups


@property
def user_or_token_accessible_streams(self):
    """Return the list of Streams a User or Token has access to."""
    if "System admin" in self.permissions:
        return Stream.query.all()
    if isinstance(self, Token):
        return self.created_by.streams
    return self.streams


User.accessible_streams = user_or_token_accessible_streams
Token.accessible_streams = user_or_token_accessible_streams


@property
def token_groups(self):
    """The groups the Token owner is a member of."""
    return self.created_by.groups


Token.groups = token_groups


class Obj(Base, ha.Point):
    """A record of an astronomical Object and its metadata, such as position,
    positional uncertainties, name, and redshift."""

    update = public

    id = sa.Column(sa.String, primary_key=True, doc="Name of the object.")
    # TODO should this column type be decimal? fixed-precison numeric

    ra_dis = sa.Column(sa.Float, doc="J2000 Right Ascension at discovery time [deg].")
    dec_dis = sa.Column(sa.Float, doc="J2000 Declination at discovery time [deg].")

    ra_err = sa.Column(
        sa.Float,
        nullable=True,
        doc="Error on J2000 Right Ascension at discovery time [deg].",
    )
    dec_err = sa.Column(
        sa.Float,
        nullable=True,
        doc="Error on J2000 Declination at discovery time [deg].",
    )

    offset = sa.Column(
        sa.Float, default=0.0, doc="Offset from nearest static object [arcsec]."
    )
    redshift = sa.Column(sa.Float, nullable=True, doc="Redshift.")
    redshift_history = sa.Column(
        JSONB,
        nullable=True,
        doc="Record of who set which redshift values and when.",
    )
    # Contains all external metadata, e.g. simbad, pan-starrs, tns, gaia
    altdata = sa.Column(
        JSONB,
        nullable=True,
        doc="Misc. alternative metadata stored in JSON format, e.g. "
        "`{'gaia': {'info': {'Teff': 5780}}}`",
    )

    dist_nearest_source = sa.Column(
        sa.Float, nullable=True, doc="Distance to the nearest Obj [arcsec]."
    )
    mag_nearest_source = sa.Column(
        sa.Float, nullable=True, doc="Magnitude of the nearest Obj [AB]."
    )
    e_mag_nearest_source = sa.Column(
        sa.Float, nullable=True, doc="Error on magnitude of the nearest Obj [mag]."
    )

    transient = sa.Column(
        sa.Boolean,
        default=False,
        doc="Boolean indicating whether the object is an astrophysical transient.",
    )
    varstar = sa.Column(
        sa.Boolean,
        default=False,
        doc="Boolean indicating whether the object is a variable star.",
    )
    is_roid = sa.Column(
        sa.Boolean,
        default=False,
        doc="Boolean indicating whether the object is a moving object.",
    )

    score = sa.Column(sa.Float, nullable=True, doc="Machine learning score.")

    origin = sa.Column(sa.String, nullable=True, doc="Origin of the object.")

    internal_key = sa.Column(
        sa.String,
        nullable=False,
        default=lambda: str(uuid.uuid4()),
        doc="Internal key used for secure websocket messaging.",
    )

    candidates = relationship(
        'Candidate',
        back_populates='obj',
        cascade='save-update, merge, refresh-expire, expunge',
        passive_deletes=True,
        order_by="Candidate.passed_at",
        doc="Candidates associated with the object.",
    )

    comments = relationship(
        'Comment',
        back_populates='obj',
        cascade='save-update, merge, refresh-expire, expunge, delete',
        passive_deletes=True,
        order_by="Comment.created_at",
        doc="Comments posted about the object.",
    )

    annotations = relationship(
        'Annotation',
        back_populates='obj',
        cascade='save-update, merge, refresh-expire, expunge',
        passive_deletes=True,
        order_by="Annotation.created_at",
        doc="Auto-annotations posted about the object.",
    )

    classifications = relationship(
        'Classification',
        back_populates='obj',
        cascade='save-update, merge, refresh-expire, expunge, delete-orphan',
        passive_deletes=True,
        order_by="Classification.created_at",
        doc="Classifications of the object.",
    )

    photometry = relationship(
        'Photometry',
        back_populates='obj',
        cascade='save-update, merge, refresh-expire, expunge',
        single_parent=True,
        passive_deletes=True,
        order_by="Photometry.mjd",
        doc="Photometry of the object.",
    )

    detect_photometry_count = sa.Column(
        sa.Integer,
        nullable=True,
        doc="How many times the object was detected above :math:`S/N = phot_detection_threshold (3.0 by default)`.",
    )

    spectra = relationship(
        'Spectrum',
        back_populates='obj',
        cascade='save-update, merge, refresh-expire, expunge, delete',
        single_parent=True,
        passive_deletes=True,
        order_by="Spectrum.observed_at",
        doc="Spectra of the object.",
    )
    thumbnails = relationship(
        'Thumbnail',
        back_populates='obj',
        cascade='save-update, merge, refresh-expire, expunge',
        passive_deletes=True,
        doc="Thumbnails of the object.",
    )

    followup_requests = relationship(
        'FollowupRequest',
        back_populates='obj',
        passive_deletes=True,
        doc="Robotic follow-up requests of the object.",
    )
    assignments = relationship(
        'ClassicalAssignment',
        back_populates='obj',
        passive_deletes=True,
        doc="Assignments of the object to classical observing runs.",
    )

    obj_notifications = relationship(
        "SourceNotification",
        back_populates="source",
        passive_deletes=True,
        doc="Notifications regarding the object sent out by users",
    )

    @hybrid_property
    def last_detected_at(self):
        """UTC ISO date at which the object was last detected above a given S/N (3.0 by default)."""
        detections = [
            phot.iso
            for phot in self.photometry
            if phot.snr is not None and phot.snr > PHOT_DETECTION_THRESHOLD
        ]
        return max(detections) if detections else None

    @last_detected_at.expression
    def last_detected_at(cls):
        """UTC ISO date at which the object was last detected above a given S/N (3.0 by default)."""
        return (
            sa.select([sa.func.max(Photometry.iso)])
            .where(Photometry.obj_id == cls.id)
            .where(Photometry.snr.isnot(None))
            .where(Photometry.snr > PHOT_DETECTION_THRESHOLD)
            .label('last_detected_at')
        )

    @hybrid_property
    def last_detected_mag(self):
        """Magnitude at which the object was last detected above a given S/N (3.0 by default)."""
        detections = [
            (phot.iso, phot.mag)
            for phot in self.photometry
            if phot.snr is not None and phot.snr > PHOT_DETECTION_THRESHOLD
        ]
        return max(detections, key=(lambda x: x[0]))[1] if detections else None

    @last_detected_mag.expression
    def last_detected_mag(cls):
        """Magnitude at which the object was last detected above a given S/N (3.0 by default)."""
        last_detected = (
            sa.select([cls.id, sa.func.max(Photometry.mjd).label("max_mjd")])
            .where(Photometry.obj_id == cls.id)
            .where(Photometry.snr.isnot(None))
            .where(Photometry.snr > PHOT_DETECTION_THRESHOLD)
            .group_by(cls.id)
            .alias()
        )
        return (
            sa.select([Photometry.mag])
            .where(Photometry.obj_id == cls.id)
            .where(Photometry.snr.isnot(None))
            .where(Photometry.snr > PHOT_DETECTION_THRESHOLD)
            .where(cls.id == last_detected.c.id)
            .where(Photometry.mjd == last_detected.c.max_mjd)
            .label('last_detected_mag')
        )

    @hybrid_property
    def peak_detected_at(self):
        """UTC ISO date at which the object was detected at peak magnitude above a given S/N (3.0 by default)."""
        detections = [
            (phot.iso, phot.mag)
            for phot in self.photometry
            if phot.snr is not None and phot.snr > PHOT_DETECTION_THRESHOLD
        ]
        return max(detections, key=(lambda x: x[1]))[0] if detections else None

    @peak_detected_at.expression
    def peak_detected_at(cls):
        """UTC ISO date at which the object was detected at peak magnitude above a given S/N (3.0 by default)."""
        return (
            sa.select([Photometry.iso])
            .where(Photometry.obj_id == cls.id)
            .where(Photometry.snr.isnot(None))
            .where(Photometry.snr > PHOT_DETECTION_THRESHOLD)
            .order_by(Photometry.mag.desc())
            .limit(1)
            .label('peak_detected_at')
        )

    @hybrid_property
    def peak_detected_mag(self):
        """Peak magnitude at which the object was detected above a given S/N (3.0 by default)."""
        detections = [
            phot.mag
            for phot in self.photometry
            if phot.snr is not None and phot.snr > PHOT_DETECTION_THRESHOLD
        ]
        return max(detections) if detections else None

    @peak_detected_mag.expression
    def peak_detected_mag(cls):
        """Peak magnitude at which the object was detected above a given S/N (3.0 by default)."""
        return (
            sa.select([sa.func.max(Photometry.mag)])
            .where(Photometry.obj_id == cls.id)
            .where(Photometry.snr.isnot(None))
            .where(Photometry.snr > PHOT_DETECTION_THRESHOLD)
            .label('peak_detected_mag')
        )

    def add_linked_thumbnails(self):
        """Determine the URLs of the SDSS and DESI DR8 thumbnails of the object,
        insert them into the Thumbnails table, and link them to the object."""
        sdss_thumb = Thumbnail(obj=self, public_url=self.sdss_url, type='sdss')
        dr8_thumb = Thumbnail(obj=self, public_url=self.desi_dr8_url, type='dr8')
        DBSession().add_all([sdss_thumb, dr8_thumb])
        DBSession().commit()

    def add_ps1_thumbnail(self):
        ps1_thumb = Thumbnail(obj=self, public_url=self.panstarrs_url, type="ps1")
        DBSession().add(ps1_thumb)
        DBSession().commit()

    @property
    def sdss_url(self):
        """Construct URL for public Sloan Digital Sky Survey (SDSS) cutout."""
        return (
            f"https://skyserver.sdss.org/dr12/SkyserverWS/ImgCutout/getjpeg"
            f"?ra={self.ra}&dec={self.dec}&scale=0.3&width=200&height=200"
            f"&opt=G&query=&Grid=on"
        )

    @property
    def desi_dr8_url(self):
        """Construct URL for public DESI DR8 cutout."""
        return (
            f"https://www.legacysurvey.org/viewer/jpeg-cutout?ra={self.ra}"
            f"&dec={self.dec}&size=200&layer=dr8&pixscale=0.262&bands=grz"
        )

    @property
    def panstarrs_url(self):
        """Construct URL for public PanSTARRS-1 (PS1) cutout.

        The cutout service doesn't allow directly querying for an image; the
        best we can do is request a page that contains a link to the image we
        want (in this case a combination of the g/r/i filters).
        """
        ps_query_url = (
            f"https://ps1images.stsci.edu/cgi-bin/ps1cutouts"
            f"?pos={self.ra}+{self.dec}&filter=color&filter=g"
            f"&filter=r&filter=i&filetypes=stack&size=250"
        )
        response = requests.get(ps_query_url)
        match = re.search('src="//ps1images.stsci.edu.*?"', response.content.decode())
        return match.group().replace('src="', 'http:').replace('"', '')

    @property
    def target(self):
        """Representation of the RA and Dec of this Obj as an
        astroplan.FixedTarget."""
        coord = ap_coord.SkyCoord(self.ra, self.dec, unit='deg')
        return astroplan.FixedTarget(name=self.id, coord=coord)

    @property
    def gal_lat_deg(self):
        """Get the galactic latitute of this object"""
        coord = ap_coord.SkyCoord(self.ra, self.dec, unit="deg")
        return coord.galactic.b.deg

    @property
    def gal_lon_deg(self):
        """Get the galactic longitude of this object"""
        coord = ap_coord.SkyCoord(self.ra, self.dec, unit="deg")
        return coord.galactic.l.deg

    @property
    def luminosity_distance(self):
        """
        The luminosity distance in Mpc, using either DM or distance data
        in the altdata fields or using the cosmology/redshift. Specifically
        the user can add `dm` (mag), `parallax` (arcsec), `dist_kpc`,
        `dist_Mpc`, `dist_pc` or `dist_cm` to `altdata` and
        those will be picked up (in that order) as the distance
        rather than the redshift.

        Return None if the redshift puts the source not within the Hubble flow
        """

        # there may be a non-redshift based measurement of distance
        # for nearby sources
        if self.altdata:
            if self.altdata.get("dm") is not None:
                # see eq (24) of https://ned.ipac.caltech.edu/level5/Hogg/Hogg7.html
                return (
                    (10 ** (float(self.altdata.get("dm")) / 5.0)) * 1e-5 * u.Mpc
                ).value
            if self.altdata.get("parallax") is not None:
                if float(self.altdata.get("parallax")) > 0:
                    # assume parallax in arcsec
                    return (1e-6 * u.Mpc / float(self.altdata.get("parallax"))).value

            if self.altdata.get("dist_kpc") is not None:
                return (float(self.altdata.get("dist_kpc")) * 1e-3 * u.Mpc).value
            if self.altdata.get("dist_Mpc") is not None:
                return (float(self.altdata.get("dist_Mpc")) * u.Mpc).value
            if self.altdata.get("dist_pc") is not None:
                return (float(self.altdata.get("dist_pc")) * 1e-6 * u.Mpc).value
            if self.altdata.get("dist_cm") is not None:
                return (float(self.altdata.get("dist_cm")) * u.Mpc / 3.085e18).value

        if self.redshift:
            if self.redshift * 2.99e5 * u.km / u.s < 350 * u.km / u.s:
                # stubbornly refuse to give a distance if the source
                # is not in the Hubble flow
                # cf. https://www.aanda.org/articles/aa/full/2003/05/aa3077/aa3077.html
                # within ~5 Mpc (cz ~ 350 km/s) a given galaxy velocty
                # can be between between ~0-500 km/s
                return None
            return (cosmo.luminosity_distance(self.redshift)).to(u.Mpc).value
        return None

    @property
    def dm(self):
        """Distance modulus to the object"""
        dl = self.luminosity_distance
        if dl:
            return 5.0 * np.log10((dl * u.Mpc) / (10 * u.pc)).value
        return None

    @property
    def angular_diameter_distance(self):
        dl = self.luminosity_distance
        if dl:
            if self.redshift and self.redshift * 2.99e5 * u.km / u.s > 350 * u.km / u.s:
                # see eq (20) of https://ned.ipac.caltech.edu/level5/Hogg/Hogg7.html
                return dl / (1 + self.redshift) ** 2
            return dl
        return None

    def airmass(self, telescope, time, below_horizon=np.inf):
        """Return the airmass of the object at a given time. Uses the Pickering
        (2002) interpolation of the Rayleigh (molecular atmosphere) airmass.

        The Pickering interpolation tends toward 38.7494 as the altitude
        approaches zero.

        Parameters
        ----------
        telescope : `skyportal.models.Telescope`
            The telescope to use for the airmass calculation
        time : `astropy.time.Time` or list of astropy.time.Time`
            The time or times at which to calculate the airmass
        below_horizon : scalar, Numeric
            Airmass value to assign when an object is below the horizon.
            An object is "below the horizon" when its altitude is less than
            zero degrees.

        Returns
        -------
        airmass : ndarray
           The airmass of the Obj at the requested times
        """

        output_shape = time.shape
        time = np.atleast_1d(time)
        altitude = self.altitude(telescope, time).to('degree').value
        above = altitude > 0

        # use Pickering (2002) interpolation to calculate the airmass
        # The Pickering interpolation tends toward 38.7494 as the altitude
        # approaches zero.
        sinarg = np.zeros_like(altitude)
        airmass = np.ones_like(altitude) * np.inf
        sinarg[above] = altitude[above] + 244 / (165 + 47 * altitude[above] ** 1.1)
        airmass[above] = 1.0 / np.sin(np.deg2rad(sinarg[above]))

        # set objects below the horizon to an airmass of infinity
        airmass[~above] = below_horizon
        airmass = airmass.reshape(output_shape)

        return airmass

    def altitude(self, telescope, time):
        """Return the altitude of the object at a given time.

        Parameters
        ----------
        telescope : `skyportal.models.Telescope`
            The telescope to use for the altitude calculation

        time : `astropy.time.Time`
            The time or times at which to calculate the altitude

        Returns
        -------
        alt : `astropy.coordinates.AltAz`
           The altitude of the Obj at the requested times
        """

        return telescope.observer.altaz(time, self.target).alt


class Filter(Base):
    """An alert filter that operates on a Stream. A Filter is associated
    with exactly one Group, and a Group may have multiple operational Filters.
    """

    # TODO: Track filter ownership and allow owners to update, delete filters
    create = read = update = delete = accessible_by_group_members

    name = sa.Column(sa.String, nullable=False, unique=False, doc="Filter name.")
    stream_id = sa.Column(
        sa.ForeignKey("streams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="ID of the Filter's Stream.",
    )
    stream = relationship(
        "Stream",
        foreign_keys=[stream_id],
        back_populates="filters",
        doc="The Filter's Stream.",
    )
    group_id = sa.Column(
        sa.ForeignKey("groups.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="ID of the Filter's Group.",
    )
    group = relationship(
        "Group",
        foreign_keys=[group_id],
        back_populates="filters",
        doc="The Filter's Group.",
    )
    candidates = relationship(
        'Candidate',
        back_populates='filter',
        cascade='save-update, merge, refresh-expire, expunge',
        passive_deletes=True,
        order_by="Candidate.passed_at",
        doc="Candidates that have passed the filter.",
    )


class Candidate(Base):
    "An Obj that passed a Filter, becoming scannable on the Filter's scanning page."
    create = read = update = delete = AccessibleIfUserMatches(
        'filter.group.group_users.user'
    )

    obj_id = sa.Column(
        sa.ForeignKey("objs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="ID of the Obj",
    )
    obj = relationship(
        "Obj",
        foreign_keys=[obj_id],
        back_populates="candidates",
        doc="The Obj that passed a filter",
    )
    filter_id = sa.Column(
        sa.ForeignKey("filters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="ID of the filter the candidate passed",
    )
    filter = relationship(
        "Filter",
        foreign_keys=[filter_id],
        back_populates="candidates",
        doc="The filter that the Candidate passed",
    )
    passed_at = sa.Column(
        sa.DateTime,
        nullable=False,
        index=True,
        doc="ISO UTC time when the Candidate passed the Filter.",
    )
    passing_alert_id = sa.Column(
        sa.BigInteger,
        index=True,
        doc="ID of the latest Stream alert that passed the Filter.",
    )
    uploader_id = sa.Column(
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="ID of the user that posted the candidate",
    )


Candidate.__table_args__ = (
    sa.Index(
        "candidates_main_index",
        Candidate.obj_id,
        Candidate.filter_id,
        Candidate.passed_at,
        unique=True,
    ),
)


def get_candidate_if_readable_by(obj_id, user_or_token, options=[]):
    """Return an Obj from the database if the Obj is a Candidate in at least
    one of the requesting User or Token owner's accessible Groups. If the Obj is not a
    Candidate in one of the User or Token owner's accessible Groups, raise an AccessError.
    If the Obj does not exist, return `None`.

    Parameters
    ----------
    obj_id : integer or string
       Primary key of the Obj.
    user_or_token : `baselayer.app.models.User` or `baselayer.app.models.Token`
       The requesting `User` or `Token` object.
    options : list of `sqlalchemy.orm.MapperOption`s
       Options that wil be passed to `options()` in the loader query.

    Returns
    -------
    obj : `skyportal.models.Obj`
       The requested Obj.
    """

    if Candidate.query.filter(Candidate.obj_id == obj_id).first() is None:
        return None
    user_group_ids = [g.id for g in user_or_token.accessible_groups]
    c = (
        Candidate.query.filter(Candidate.obj_id == obj_id)
        .filter(
            Candidate.filter_id.in_(
                DBSession.query(Filter.id).filter(Filter.group_id.in_(user_group_ids))
            )
        )
        .options(options)
        .first()
    )
    if c is None:
        raise AccessError("Insufficient permissions.")
    return c.obj


def candidate_is_readable_by(self, user_or_token):
    """Return a boolean indicating whether the Candidate passed the Filter
    of any of a User or Token owner's accessible Groups.


    Parameters
    ----------
    user_or_token : `baselayer.app.models.User` or `baselayer.app.models.Token`
       The requesting `User` or `Token` object.

    Returns
    -------
    readable : bool
       Whether the Candidate is readable by the User or Token owner.
    """
    return self.filter.group in user_or_token.accessible_groups


Candidate.get_obj_if_readable_by = get_candidate_if_readable_by
Candidate.is_readable_by = candidate_is_readable_by


User.listings = relationship(
    'Listing',
    back_populates='user',
    passive_deletes=True,
    doc='The listings saved by this user',
)


Source = join_model("sources", Group, Obj)
Source.create = Source.read = Source.update = Source.delete = (
    accessible_by_group_members & Source.read
)

Source.__doc__ = (
    "An Obj that has been saved to a Group. Once an Obj is saved as a Source, "
    "the Obj is shielded in perpetuity from automatic database removal. "
    "If a Source is 'unsaved', its 'active' flag is set to False, but "
    "it is not purged."
)

Source.saved_by_id = sa.Column(
    sa.ForeignKey("users.id"),
    nullable=True,
    unique=False,
    index=True,
    doc="ID of the User that saved the Obj to its Group.",
)
Source.saved_by = relationship(
    "User",
    foreign_keys=[Source.saved_by_id],
    backref="saved_sources",
    doc="User that saved the Obj to its Group.",
)
Source.saved_at = sa.Column(
    sa.DateTime,
    nullable=False,
    default=utcnow,
    index=True,
    doc="ISO UTC time when the Obj was saved to its Group.",
)
Source.active = sa.Column(
    sa.Boolean,
    server_default="true",
    doc="Whether the Obj is still 'active' as a Source in its Group. "
    "If this flag is set to False, the Source will not appear in the Group's "
    "sample.",
)
Source.requested = sa.Column(
    sa.Boolean,
    server_default="false",
    doc="True if the source has been shared with another Group, but not saved "
    "by the recipient Group.",
)

Source.unsaved_by_id = sa.Column(
    sa.ForeignKey("users.id"),
    nullable=True,
    unique=False,
    index=True,
    doc="ID of the User who unsaved the Source.",
)
Source.unsaved_by = relationship(
    "User", foreign_keys=[Source.unsaved_by_id], doc="User who unsaved the Source."
)
Source.unsaved_at = sa.Column(
    sa.DateTime,
    nullable=True,
    doc="ISO UTC time when the Obj was unsaved from Group.",
)

Obj.sources = relationship(
    Source,
    back_populates='obj',
    passive_deletes=True,
    doc="Instances in which a group saved this Obj.",
)
Obj.candidates = relationship(
    Candidate,
    back_populates='obj',
    passive_deletes=True,
    doc="Instances in which this Obj passed a group's filter.",
)


def source_is_readable_by(self, user_or_token):
    """Return a boolean indicating whether the Source has been saved to
    any of a User or Token owner's accessible Groups.

    Parameters
    ----------
    user_or_token : `baselayer.app.models.User` or `baselayer.app.models.Token`
       The requesting `User` or `Token` object.

    Returns
    -------
    readable : bool
       Whether the Candidate is readable by the User or Token owner.
    """

    source_group_ids = [
        row[0]
        for row in DBSession.query(Source.group_id)
        .filter(Source.obj_id == self.obj_id)
        .all()
    ]
    return bool(set(source_group_ids) & {g.id for g in user_or_token.accessible_groups})


def get_source_if_readable_by(obj_id, user_or_token, options=[]):
    """Return an Obj from the database if the Obj is a Source in at least
    one of the requesting User or Token owner's accessible Groups. If the Obj is not a
    Source in one of the User or Token owner's accessible Groups, raise an AccessError.
    If the Obj does not exist, return `None`.

    Parameters
    ----------
    obj_id : integer or string
       Primary key of the Obj.
    user_or_token : `baselayer.app.models.User` or `baselayer.app.models.Token`
       The requesting `User` or `Token` object.
    options : list of `sqlalchemy.orm.MapperOption`s
       Options that wil be passed to `options()` in the loader query.

    Returns
    -------
    obj : `skyportal.models.Obj`
       The requested Obj.
    """

    if Obj.query.get(obj_id) is None:
        return None
    user_group_ids = [g.id for g in user_or_token.accessible_groups]
    s = (
        Source.query.filter(Source.obj_id == obj_id)
        .filter(Source.group_id.in_(user_group_ids))
        .options(options)
        .first()
    )
    if s is None:
        raise AccessError("Insufficient permissions.")
    return s.obj


Source.is_readable_by = source_is_readable_by
Source.get_obj_if_readable_by = get_source_if_readable_by


def get_obj_if_readable_by(obj_id, user_or_token, options=[]):
    """Return an Obj from the database if the Obj is either a Source or a Candidate in at least
    one of the requesting User or Token owner's accessible Groups. If the Obj is not a
    Source or a Candidate in one of the User or Token owner's accessible Groups, raise an AccessError.
    If the Obj does not exist, return `None`.

    Parameters
    ----------
    obj_id : integer or string
       Primary key of the Obj.
    user_or_token : `baselayer.app.models.User` or `baselayer.app.models.Token`
       The requesting `User` or `Token` object.
    options : list of `sqlalchemy.orm.MapperOption`s
       Options that wil be passed to `options()` in the loader query.

    Returns
    -------
    obj : `skyportal.models.Obj`
       The requested Obj.
    """

    def construct_joinedload(base, additional_attrs):
        jl = joinedload(base)
        for attr in additional_attrs:
            jl = jl.joinedload(attr)
        return jl

    if Obj.query.get(obj_id) is None:
        return None
    if "System admin" in user_or_token.permissions:
        return Obj.query.options(options).get(obj_id)

    # the order of the following attempts is important -
    # this one should come first
    if Obj.get_photometry_readable_by_user(obj_id, user_or_token):
        return Obj.query.options(options).get(obj_id)

    try:
        source_opts = [construct_joinedload(Source.obj, o.path) for o in options]
        obj = Source.get_obj_if_readable_by(obj_id, user_or_token, source_opts)
    except AccessError:  # They may still be able to view the associated Candidate
        cand_opts = [construct_joinedload(Candidate.obj, o.path) for o in options]
        obj = Candidate.get_obj_if_readable_by(obj_id, user_or_token, cand_opts)

    if obj is None:
        raise AccessError('Insufficient permissions.')

    # If we get here, the user has access to either the associated Source or Candidate
    return obj


Obj.get_if_readable_by = get_obj_if_readable_by


def get_obj_comments_readable_by(self, user_or_token):
    """Query the database and return the Comments on this Obj that are accessible
    to any of the User or Token owner's accessible Groups.

    Parameters
    ----------
    user_or_token : `baselayer.app.models.User` or `baselayer.app.models.Token`
       The requesting `User` or `Token` object.

    Returns
    -------
    comment_list : list of `skyportal.models.Comment`
       The accessible comments attached to this Obj.
    """
    readable_comments = [
        comment for comment in self.comments if comment.is_readable_by(user_or_token)
    ]

    # Grab basic author info for the comments
    for comment in readable_comments:
        comment.author_info = comment.construct_author_info_dict()

    return readable_comments


Obj.get_comments_readable_by = get_obj_comments_readable_by


def get_obj_annotations_readable_by(self, user_or_token):
    """Query the database and return the Annotations on this Obj that are accessible
    to any of the User or Token owner's accessible Groups.

    Parameters
    ----------
    user_or_token : `baselayer.app.models.User` or `baselayer.app.models.Token`
       The requesting `User` or `Token` object.

    Returns
    -------
    annotation_list : list of `skyportal.models.Annotation`
       The accessible annotations attached to this Obj.
    """
    readable_annotations = [
        annotation
        for annotation in self.annotations
        if annotation.is_readable_by(user_or_token)
    ]

    # Grab basic author info for the annotations
    for annotation in readable_annotations:
        annotation.author_info = annotation.construct_author_info_dict()

    return readable_annotations


Obj.get_annotations_readable_by = get_obj_annotations_readable_by


def get_obj_classifications_readable_by(self, user_or_token):
    """Query the database and return the Classifications on this Obj that are accessible
    to any of the User or Token owner's accessible Groups.

    Parameters
    ----------
    user_or_token : `baselayer.app.models.User` or `baselayer.app.models.Token`
       The requesting `User` or `Token` object.

    Returns
    -------
    classification_list : list of `skyportal.models.Classification`
       The accessible classifications attached to this Obj.
    """
    return [
        classifications
        for classifications in self.classifications
        if classifications.is_readable_by(user_or_token)
    ]


Obj.get_classifications_readable_by = get_obj_classifications_readable_by


def get_photometry_readable_by_user(obj_id, user_or_token):
    """Query the database and return the Photometry for this Obj that is shared
    with any of the User or Token owner's accessible Groups.

    Parameters
    ----------
    obj_id : string
       The ID of the Obj to look up.
    user_or_token : `baselayer.app.models.User` or `baselayer.app.models.Token`
       The requesting `User` or `Token` object.

    Returns
    -------
    photometry_list : list of `skyportal.models.Photometry`
       The accessible Photometry of this Obj.
    """
    return (
        Photometry.query.filter(Photometry.obj_id == obj_id)
        .filter(
            Photometry.groups.any(
                Group.id.in_([g.id for g in user_or_token.accessible_groups])
            )
        )
        .all()
    )


Obj.get_photometry_readable_by_user = get_photometry_readable_by_user


def get_spectra_readable_by(obj_id, user_or_token, options=()):
    """Query the database and return the Spectra for this Obj that are shared
    with any of the User or Token owner's accessible Groups.

    Parameters
    ----------
    obj_id : string
       The ID of the Obj to look up.
    user_or_token : `baselayer.app.models.User` or `baselayer.app.models.Token`
       The requesting `User` or `Token` object.
    options : list of `sqlalchemy.orm.MapperOption`s
       Options that wil be passed to `options()` in the loader query.

    Returns
    -------
    photometry_list : list of `skyportal.models.Spectrum`
       The accessible Spectra of this Obj.
    """

    return (
        Spectrum.query.filter(Spectrum.obj_id == obj_id)
        .filter(
            Spectrum.groups.any(
                Group.id.in_([g.id for g in user_or_token.accessible_groups])
            )
        )
        .options(options)
        .all()
    )


Obj.get_spectra_readable_by = get_spectra_readable_by


User.sources = relationship(
    'Obj',
    backref='users',
    secondary='join(Group, sources).join(group_users)',
    primaryjoin='group_users.c.user_id == users.c.id',
    doc='The Sources accessible to this User.',
    viewonly=True,
)

isadmin = property(lambda self: "System admin" in self.permissions)
User.is_system_admin = isadmin
Token.is_system_admin = isadmin


class SourceView(Base):
    """Record of an instance in which a Source was viewed via the frontend or
    retrieved via the API (for use in the "Top Sources" widget).
    """

    obj_id = sa.Column(
        sa.ForeignKey('objs.id', ondelete='CASCADE'),
        nullable=False,
        unique=False,
        index=True,
        doc="Object ID for which the view was registered.",
    )
    username_or_token_id = sa.Column(
        sa.String,
        nullable=False,
        unique=False,
        doc="Username or token ID of the viewer.",
    )
    is_token = sa.Column(
        sa.Boolean,
        nullable=False,
        default=False,
        doc="Whether the viewer was a User or a Token.",
    )
    created_at = sa.Column(
        sa.DateTime,
        nullable=False,
        default=datetime.utcnow,
        index=True,
        doc="UTC timestamp of the view.",
    )


class Telescope(Base):
    """A ground or space-based observational facility that can host Instruments."""

    name = sa.Column(
        sa.String,
        unique=True,
        nullable=False,
        doc="Unabbreviated facility name (e.g., Palomar 200-inch Hale Telescope).",
    )
    nickname = sa.Column(
        sa.String, nullable=False, doc="Abbreviated facility name (e.g., P200)."
    )
    lat = sa.Column(sa.Float, nullable=True, doc='Latitude in deg.')
    lon = sa.Column(sa.Float, nullable=True, doc='Longitude in deg.')
    elevation = sa.Column(sa.Float, nullable=True, doc='Elevation in meters.')
    diameter = sa.Column(sa.Float, nullable=False, doc='Diameter in meters.')
    skycam_link = sa.Column(
        URLType, nullable=True, doc="Link to the telescope's sky camera."
    )
    robotic = sa.Column(
        sa.Boolean, default=False, nullable=False, doc="Is this telescope robotic?"
    )

    fixed_location = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default='true',
        doc="Does this telescope have a fixed location (lon, lat, elev)?",
    )

    weather = sa.Column(JSONB, nullable=True, doc='Latest weather information')
    weather_retrieved_at = sa.Column(
        sa.DateTime, nullable=True, doc="When was the weather last retrieved?"
    )
    weather_link = sa.Column(
        URLType, nullable=True, doc="Link to the preferred weather site."
    )

    instruments = relationship(
        'Instrument',
        back_populates='telescope',
        cascade='save-update, merge, refresh-expire, expunge',
        passive_deletes=True,
        doc="The Instruments on this telescope.",
    )

    @property
    def observer(self):
        """Return an `astroplan.Observer` representing an observer at this
        facility, accounting for the latitude, longitude, elevation, and
        local time zone of the observatory (if ground based)."""
        try:
            return self._observer
        except AttributeError:
            tf = timezonefinder.TimezoneFinder(in_memory=True)
            local_tz = tf.closest_timezone_at(
                lng=self.lon, lat=self.lat, delta_degree=5
            )
            self._observer = astroplan.Observer(
                longitude=self.lon * u.deg,
                latitude=self.lat * u.deg,
                elevation=self.elevation * u.m,
                timezone=local_tz,
            )
        return self._observer

    def next_sunset(self, time=None):
        """The astropy timestamp of the next sunset after `time` at this site.
        If time=None, uses the current time."""
        if time is None:
            time = ap_time.Time.now()
        observer = self.observer
        return observer.sun_set_time(time, which='next')

    def next_sunrise(self, time=None):
        """The astropy timestamp of the next sunrise after `time` at this site.
        If time=None, uses the current time."""
        if time is None:
            time = ap_time.Time.now()
        observer = self.observer
        return observer.sun_rise_time(time, which='next')

    def next_twilight_evening_nautical(self, time=None):
        """The astropy timestamp of the next evening nautical (-12 degree)
        twilight at this site. If time=None, uses the current time."""
        if time is None:
            time = ap_time.Time.now()
        observer = self.observer
        return observer.twilight_evening_nautical(time, which='next')

    def next_twilight_morning_nautical(self, time=None):
        """The astropy timestamp of the next morning nautical (-12 degree)
        twilight at this site. If time=None, uses the current time."""
        if time is None:
            time = ap_time.Time.now()
        observer = self.observer
        return observer.twilight_morning_nautical(time, which='next')

    def next_twilight_evening_astronomical(self, time=None):
        """The astropy timestamp of the next evening astronomical (-18 degree)
        twilight at this site. If time=None, uses the current time."""
        if time is None:
            time = ap_time.Time.now()
        observer = self.observer
        return observer.twilight_evening_astronomical(time, which='next')

    def next_twilight_morning_astronomical(self, time=None):
        """The astropy timestamp of the next morning astronomical (-18 degree)
        twilight at this site. If time=None, uses the current time."""
        if time is None:
            time = ap_time.Time.now()
        observer = self.observer
        return observer.twilight_morning_astronomical(time, which='next')

    def ephemeris(self, time):

        sunrise = self.next_sunrise(time=time)
        sunset = self.next_sunset(time=time)

        if sunset > sunrise:
            sunset = self.observer.sun_set_time(time, which='previous')
            time = sunset - ap_time.TimeDelta(30, format='sec')

        twilight_morning_astronomical = self.next_twilight_morning_astronomical(
            time=time
        )
        twilight_evening_astronomical = self.next_twilight_evening_astronomical(
            time=time
        )

        twilight_morning_nautical = self.next_twilight_morning_nautical(time=time)
        twilight_evening_nautical = self.next_twilight_evening_nautical(time=time)

        return {
            'sunset_utc': sunset.isot,
            'sunrise_utc': sunrise.isot,
            'twilight_morning_astronomical_utc': twilight_morning_astronomical.isot,
            'twilight_evening_astronomical_utc': twilight_evening_astronomical.isot,
            'twilight_morning_nautical_utc': twilight_morning_nautical.isot,
            'twilight_evening_nautical_utc': twilight_evening_nautical.isot,
            'utc_offset_hours': self.observer.timezone.utcoffset(time.datetime)
            / timedelta(hours=1),
            'sunset_unix_ms': sunset.unix * 1000,
            'sunrise_unix_ms': sunrise.unix * 1000,
            'twilight_morning_astronomical_unix_ms': twilight_morning_astronomical.unix
            * 1000,
            'twilight_evening_astronomical_unix_ms': twilight_evening_astronomical.unix
            * 1000,
            'twilight_morning_nautical_unix_ms': twilight_morning_nautical.unix * 1000,
            'twilight_evening_nautical_unix_ms': twilight_evening_nautical.unix * 1000,
        }


class ArrayOfEnum(ARRAY):
    def bind_expression(self, bindvalue):
        return cast(bindvalue, self)

    def result_processor(self, dialect, coltype):
        super_rp = super(ArrayOfEnum, self).result_processor(dialect, coltype)

        def handle_raw_string(value):
            if value is None or value == '{}':  # 2nd case, empty array
                return []
            inner = re.match(r"^{(.*)}$", value).group(1)
            return inner.split(",")

        def process(value):
            return super_rp(handle_raw_string(value))

        return process


class Instrument(Base):
    """An instrument attached to a telescope."""

    name = sa.Column(sa.String, unique=True, nullable=False, doc="Instrument name.")
    type = sa.Column(
        instrument_types,
        nullable=False,
        doc="Instrument type, one of Imager, Spectrograph, or Imaging Spectrograph.",
    )

    band = sa.Column(
        sa.String,
        doc="The spectral band covered by the instrument " "(e.g., Optical, IR).",
    )
    telescope_id = sa.Column(
        sa.ForeignKey('telescopes.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="The ID of the Telescope that hosts the Instrument.",
    )
    telescope = relationship(
        'Telescope',
        back_populates='instruments',
        doc="The Telescope that hosts the Instrument.",
    )

    photometry = relationship(
        'Photometry',
        back_populates='instrument',
        passive_deletes=True,
        doc="The Photometry produced by this instrument.",
    )
    spectra = relationship(
        'Spectrum',
        back_populates='instrument',
        passive_deletes=True,
        doc="The Spectra produced by this instrument.",
    )

    # can be [] if an instrument is spec only
    filters = sa.Column(
        ArrayOfEnum(allowed_bandpasses),
        nullable=False,
        default=[],
        doc='List of filters on the instrument (if any).',
    )

    allocations = relationship(
        'Allocation',
        back_populates="instrument",
        cascade="save-update, merge, refresh-expire, expunge",
        passive_deletes=True,
    )
    observing_runs = relationship(
        'ObservingRun',
        back_populates='instrument',
        passive_deletes=True,
        doc="List of ObservingRuns on the Instrument.",
    )

    api_classname = sa.Column(
        api_classnames, nullable=True, doc="Name of the instrument's API class."
    )

    listener_classname = sa.Column(
        listener_classnames,
        nullable=True,
        doc="Name of the instrument's listener class.",
    )

    test_column = sa.Column(
        sa.String,
        doc="Test column.",
    )

    @property
    def does_spectroscopy(self):
        """Return a boolean indicating whether the instrument is capable of
        performing spectroscopy."""
        return 'spec' in self.type

    @property
    def does_imaging(self):
        """Return a boolean indicating whether the instrument is capable of
        performing imaging."""
        return 'imag' in self.type

    @property
    def api_class(self):
        return getattr(facility_apis, self.api_classname)

    @property
    def listener_class(self):
        return getattr(facility_apis, self.listener_classname)


class Allocation(Base):
    """An allocation of observing time on a robotic instrument."""

    create = (
        read
    ) = (
        update
    ) = delete = accessible_by_group_members & AccessibleIfRelatedRowsAreAccessible(
        instrument='read'
    )

    pi = sa.Column(sa.String, doc="The PI of the allocation's proposal.")
    proposal_id = sa.Column(
        sa.String, doc="The ID of the proposal associated with this allocation."
    )
    start_date = sa.Column(sa.DateTime, doc='The UTC start date of the allocation.')
    end_date = sa.Column(sa.DateTime, doc='The UTC end date of the allocation.')
    hours_allocated = sa.Column(
        sa.Float, nullable=False, doc='The number of hours allocated.'
    )
    requests = relationship(
        'FollowupRequest',
        back_populates='allocation',
        doc='The requests made against this allocation.',
    )

    group_id = sa.Column(
        sa.ForeignKey('groups.id', ondelete='CASCADE'),
        index=True,
        doc='The ID of the Group the allocation is associated with.',
        nullable=False,
    )
    group = relationship(
        'Group',
        back_populates='allocations',
        doc='The Group the allocation is associated with.',
    )

    instrument_id = sa.Column(
        sa.ForeignKey('instruments.id', ondelete='CASCADE'),
        index=True,
        doc="The ID of the Instrument the allocation is associated with.",
        nullable=False,
    )
    instrument = relationship(
        'Instrument',
        back_populates='allocations',
        doc="The Instrument the allocation is associated with.",
    )

    _altdata = sa.Column(
        EncryptedType(JSONType, cfg['app.secret_key'], AesEngine, 'pkcs5')
    )

    @property
    def altdata(self):
        if self._altdata is None:
            return {}
        else:
            return json.loads(self._altdata)

    @altdata.setter
    def altdata(self, value):
        self._altdata = value


class Taxonomy(Base):
    """An ontology within which Objs can be classified."""

    # TODO: Add ownership logic to taxonomy
    read = accessible_by_groups_members

    __tablename__ = 'taxonomies'
    name = sa.Column(
        sa.String,
        nullable=False,
        doc='Short string to make this taxonomy memorable to end users.',
    )
    hierarchy = sa.Column(
        JSONB,
        nullable=False,
        doc='Nested JSON describing the taxonomy '
        'which should be validated against '
        'a schema before entry.',
    )
    provenance = sa.Column(
        sa.String,
        nullable=True,
        doc='Identifier (e.g., URL or git hash) that '
        'uniquely ties this taxonomy back '
        'to an origin or place of record.',
    )
    version = sa.Column(
        sa.String, nullable=False, doc='Semantic version of this taxonomy'
    )

    isLatest = sa.Column(
        sa.Boolean,
        default=True,
        nullable=False,
        doc='Consider this the latest version of '
        'the taxonomy with this name? Defaults '
        'to True.',
    )
    groups = relationship(
        "Group",
        secondary="group_taxonomy",
        cascade="save-update," "merge, refresh-expire, expunge",
        passive_deletes=True,
        doc="List of Groups that have access to this Taxonomy.",
    )

    classifications = relationship(
        'Classification',
        back_populates='taxonomy',
        cascade='save-update, merge, refresh-expire, expunge',
        passive_deletes=True,
        order_by="Classification.created_at",
        doc="Classifications made within this Taxonomy.",
    )


GroupTaxonomy = join_model("group_taxonomy", Group, Taxonomy)
GroupTaxonomy.__doc__ = "Join table mapping Groups to Taxonomies."
GroupTaxonomy.delete = GroupTaxonomy.update = (
    accessible_by_group_admins & GroupTaxonomy.read
)


def get_taxonomy_usable_by_user(taxonomy_id, user_or_token):
    """Query the database and return the requested Taxonomy if it is accessible
    to the requesting User or Token owner. If the Taxonomy is not accessible or
    if it does not exist, return an empty list.

    Parameters
    ----------
    taxonomy_id : integer
       The ID of the requested Taxonomy.
    user_or_token : `baselayer.app.models.User` or `baselayer.app.models.Token`
       The requesting `User` or `Token` object.

    Returns
    -------
    tax : `skyportal.models.Taxonomy`
       The requested Taxonomy.
    """

    return (
        Taxonomy.query.filter(Taxonomy.id == taxonomy_id)
        .filter(
            Taxonomy.groups.any(
                Group.id.in_([g.id for g in user_or_token.accessible_groups])
            )
        )
        .all()
    )


Taxonomy.get_taxonomy_usable_by_user = get_taxonomy_usable_by_user


class Comment(Base):
    """A comment made by a User or a Robot (via the API) on a Source."""

    create = AccessibleIfRelatedRowsAreAccessible(obj='read')

    read = accessible_by_groups_members & AccessibleIfRelatedRowsAreAccessible(
        obj='read'
    )

    update = delete = AccessibleIfUserMatches('author')

    text = sa.Column(sa.String, nullable=False, doc="Comment body.")
    ctype = sa.Column(
        sa.Enum('text', 'redshift', name='comment_types', validate_strings=True),
        doc="Comment type. Can be one of 'text' or 'redshift'.",
    )

    attachment_name = sa.Column(
        sa.String, nullable=True, doc="Filename of the attachment."
    )

    attachment_bytes = sa.Column(
        sa.types.LargeBinary,
        nullable=True,
        doc="Binary representation of the attachment.",
    )

    origin = sa.Column(sa.String, nullable=True, doc='Comment origin.')
    author = relationship(
        "User",
        back_populates="comments",
        doc="Comment's author.",
        uselist=False,
        foreign_keys="Comment.author_id",
    )
    author_id = sa.Column(
        sa.ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="ID of the Comment author's User instance.",
    )
    obj_id = sa.Column(
        sa.ForeignKey('objs.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="ID of the Comment's Obj.",
    )
    obj = relationship(
        'Obj',
        back_populates='comments',
        doc="The Comment's Obj.",
    )
    groups = relationship(
        "Group",
        secondary="group_comments",
        cascade="save-update, merge, refresh-expire, expunge",
        passive_deletes=True,
        doc="Groups that can see the comment.",
    )

    def construct_author_info_dict(self):
        return {
            field: getattr(self.author, field)
            for field in ('username', 'first_name', 'last_name', 'gravatar_url')
        }

    @classmethod
    def get_if_readable_by(cls, ident, user, options=[]):
        comment = cls.query.options(options).get(ident)

        if comment is not None and not comment.is_readable_by(user):
            raise AccessError('Insufficient permissions.')

        # Grab basic author info for the comment
        comment.author_info = comment.construct_author_info_dict()

        return comment


GroupComment = join_model("group_comments", Group, Comment)
GroupComment.__doc__ = "Join table mapping Groups to Comments."
GroupComment.delete = GroupComment.update = (
    accessible_by_group_admins & GroupComment.read
)

User.comments = relationship(
    "Comment",
    back_populates="author",
    foreign_keys="Comment.author_id",
    cascade="delete",
    passive_deletes=True,
)


class Annotation(Base):
    """A sortable/searchable Annotation made by a filter or other robot,
    with a set of data as JSON"""

    create = AccessibleIfRelatedRowsAreAccessible(obj='read')
    read = accessible_by_groups_members & AccessibleIfRelatedRowsAreAccessible(
        obj='read'
    )
    update = delete = AccessibleIfUserMatches('author')

    __table_args__ = (UniqueConstraint('obj_id', 'origin'),)

    data = sa.Column(JSONB, default=None, doc="Searchable data in JSON format")
    author = relationship(
        "User",
        back_populates="annotations",
        doc="Annotation's author.",
        uselist=False,
        foreign_keys="Annotation.author_id",
    )
    author_id = sa.Column(
        sa.ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="ID of the Annotation author's User instance.",
    )

    origin = sa.Column(
        sa.String,
        index=True,
        nullable=False,
        doc=(
            'What generated the annotation. This should generally map to a '
            'filter/group name. But since an annotation can be made accessible to multiple '
            'groups, the origin name does not necessarily have to map to a single group name.'
            ' The important thing is to make the origin distinct and descriptive such '
            'that annotations from the same origin generally have the same metrics. One '
            'annotation with multiple fields from each origin is allowed.'
        ),
    )
    obj_id = sa.Column(
        sa.ForeignKey('objs.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="ID of the Annotation's Obj.",
    )

    obj = relationship('Obj', back_populates='annotations', doc="The Annotation's Obj.")
    groups = relationship(
        "Group",
        secondary="group_annotations",
        cascade="save-update, merge, refresh-expire, expunge",
        passive_deletes=True,
        doc="Groups that can see the annotation.",
    )

    def construct_author_info_dict(self):
        return {
            field: getattr(self.author, field)
            for field in ('username', 'first_name', 'last_name', 'gravatar_url')
        }

    @classmethod
    def get_if_readable_by(cls, ident, user, options=[]):
        annotation = cls.query.options(options).get(ident)

        if annotation is not None and not annotation.is_readable_by(user):
            raise AccessError('Insufficient permissions.')

        # Grab basic author info for the annotation
        annotation.author_info = annotation.construct_author_info_dict()

        return annotation

    __table_args__ = (UniqueConstraint('obj_id', 'origin'),)


GroupAnnotation = join_model("group_annotations", Group, Annotation)
GroupAnnotation.__doc__ = "Join table mapping Groups to Annotation."
GroupAnnotation.delete = GroupAnnotation.update = (
    accessible_by_group_admins & GroupAnnotation.read
)

User.annotations = relationship(
    "Annotation", back_populates="author", foreign_keys="Annotation.author_id"
)

# To create or read a classification, you must have read access to the
# underlying taxonomy, and be a member of at least one of the
# classification's target groups
ok_if_tax_and_obj_readable = AccessibleIfRelatedRowsAreAccessible(
    taxonomy='read', obj='read'
)


class Classification(Base):
    """Classification of an Obj."""

    create = ok_if_tax_and_obj_readable
    read = accessible_by_groups_members & ok_if_tax_and_obj_readable
    update = delete = AccessibleIfUserMatches('author')

    classification = sa.Column(sa.String, nullable=False, doc="The assigned class.")
    taxonomy_id = sa.Column(
        sa.ForeignKey('taxonomies.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="ID of the Taxonomy in which this Classification was made.",
    )
    taxonomy = relationship(
        'Taxonomy',
        back_populates='classifications',
        doc="Taxonomy in which this Classification was made.",
    )
    probability = sa.Column(
        sa.Float,
        doc='User-assigned probability of belonging to this class',
        nullable=True,
    )

    author_id = sa.Column(
        sa.ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="ID of the User that made this Classification",
    )
    author = relationship('User', doc="The User that made this classification.")
    author_name = sa.Column(
        sa.String,
        nullable=False,
        doc="User.username or Token.id " "of the Classification's author.",
    )
    obj_id = sa.Column(
        sa.ForeignKey('objs.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="ID of the Classification's Obj.",
    )
    obj = relationship(
        'Obj', back_populates='classifications', doc="The Classification's Obj."
    )
    groups = relationship(
        "Group",
        secondary="group_classifications",
        cascade="save-update, merge, refresh-expire, expunge",
        passive_deletes=True,
        doc="Groups that can access this Classification.",
    )


GroupClassification = join_model("group_classifications", Group, Classification)
GroupClassification.__doc__ = "Join table mapping Groups to Classifications."
GroupClassification.delete = GroupClassification.update = (
    accessible_by_group_admins & GroupClassification.read
)


class Photometry(ha.Point, Base):
    """Calibrated measurement of the flux of an object through a broadband filter."""

    __tablename__ = 'photometry'

    read = accessible_by_groups_members
    update = delete = accessible_by_owner

    mjd = sa.Column(sa.Float, nullable=False, doc='MJD of the observation.', index=True)
    flux = sa.Column(
        sa.Float,
        doc='Flux of the observation in µJy. '
        'Corresponds to an AB Zeropoint of 23.9 in all '
        'filters.',
        server_default='NaN',
        nullable=False,
    )

    fluxerr = sa.Column(
        sa.Float, nullable=False, doc='Gaussian error on the flux in µJy.'
    )
    filter = sa.Column(
        allowed_bandpasses,
        nullable=False,
        doc='Filter with which the observation was taken.',
    )

    ra_unc = sa.Column(sa.Float, doc="Uncertainty of ra position [arcsec]")
    dec_unc = sa.Column(sa.Float, doc="Uncertainty of dec position [arcsec]")

    original_user_data = sa.Column(
        JSONB,
        doc='Original data passed by the user '
        'through the PhotometryHandler.POST '
        'API or the PhotometryHandler.PUT '
        'API. The schema of this JSON '
        'validates under either '
        'schema.PhotometryFlux or schema.PhotometryMag '
        '(depending on how the data was passed).',
    )
    altdata = sa.Column(JSONB, doc="Arbitrary metadata in JSON format..")
    upload_id = sa.Column(
        sa.String,
        nullable=False,
        default=lambda: str(uuid.uuid4()),
        doc="ID of the batch in which this Photometry was uploaded (for bulk deletes).",
    )
    origin = sa.Column(
        sa.String,
        nullable=False,
        unique=False,
        index=True,
        doc="Origin from which this Photometry was extracted (if any).",
        server_default='',
    )

    obj_id = sa.Column(
        sa.ForeignKey('objs.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="ID of the Photometry's Obj.",
    )
    obj = relationship('Obj', back_populates='photometry', doc="The Photometry's Obj.")
    groups = relationship(
        "Group",
        secondary="group_photometry",
        back_populates="photometry",
        cascade="save-update, merge, refresh-expire, expunge",
        passive_deletes=True,
        doc="Groups that can access this Photometry.",
    )
    instrument_id = sa.Column(
        sa.ForeignKey('instruments.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="ID of the Instrument that took this Photometry.",
    )
    instrument = relationship(
        'Instrument',
        back_populates='photometry',
        doc="Instrument that took this Photometry.",
    )

    followup_request_id = sa.Column(
        sa.ForeignKey('followuprequests.id'), nullable=True, index=True
    )
    followup_request = relationship('FollowupRequest', back_populates='photometry')

    assignment_id = sa.Column(
        sa.ForeignKey('classicalassignments.id'), nullable=True, index=True
    )
    assignment = relationship('ClassicalAssignment', back_populates='photometry')

    owner_id = sa.Column(
        sa.ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="ID of the User who uploaded the photometry.",
    )
    owner = relationship(
        'User',
        back_populates='photometry',
        foreign_keys=[owner_id],
        cascade='save-update, merge, refresh-expire, expunge',
        doc="The User who uploaded the photometry.",
    )

    @hybrid_property
    def mag(self):
        """The magnitude of the photometry point in the AB system."""
        if not np.isnan(self.flux) and self.flux > 0:
            return -2.5 * np.log10(self.flux) + PHOT_ZP
        else:
            return None

    @hybrid_property
    def e_mag(self):
        """The error on the magnitude of the photometry point."""
        if not np.isnan(self.flux) and self.flux > 0 and self.fluxerr > 0:
            return (2.5 / np.log(10)) * (self.fluxerr / self.flux)
        else:
            return None

    @mag.expression
    def mag(cls):
        """The magnitude of the photometry point in the AB system."""
        return sa.case(
            [
                (
                    sa.and_(cls.flux != 'NaN', cls.flux > 0),  # noqa
                    -2.5 * sa.func.log(cls.flux) + PHOT_ZP,
                )
            ],
            else_=None,
        )

    @e_mag.expression
    def e_mag(cls):
        """The error on the magnitude of the photometry point."""
        return sa.case(
            [
                (
                    sa.and_(
                        cls.flux != 'NaN', cls.flux > 0, cls.fluxerr > 0
                    ),  # noqa: E711
                    2.5 / sa.func.ln(10) * cls.fluxerr / cls.flux,
                )
            ],
            else_=None,
        )

    @hybrid_property
    def jd(self):
        """Julian Date of the exposure that produced this Photometry."""
        return self.mjd + 2_400_000.5

    @hybrid_property
    def iso(self):
        """UTC ISO timestamp (ArrowType) of the exposure that produced this Photometry."""
        return arrow.get((self.mjd - 40_587) * 86400.0)

    @iso.expression
    def iso(cls):
        """UTC ISO timestamp (ArrowType) of the exposure that produced this Photometry."""
        # converts MJD to unix timestamp
        return sa.func.to_timestamp((cls.mjd - 40_587) * 86400.0)

    @hybrid_property
    def snr(self):
        """Signal-to-noise ratio of this Photometry point."""
        return (
            self.flux / self.fluxerr
            if not np.isnan(self.flux) and not np.isnan(self.fluxerr)
            else None
        )

    @snr.expression
    def snr(self):
        """Signal-to-noise ratio of this Photometry point."""
        return sa.case(
            [
                (
                    sa.and_(self.flux != 'NaN', self.fluxerr != 0),  # noqa
                    self.flux / self.fluxerr,
                )
            ],
            else_=None,
        )


Photometry.is_modifiable_by = is_modifiable_by

# Deduplication index. This is a unique index that prevents any photometry
# point that has the same obj_id, instrument_id, origin, mjd, flux error,
# and flux as a photometry point that already exists within the table from
# being inserted into the table. The index also allows fast lookups on this
# set of columns, making the search for duplicates a O(log(n)) operation.

Photometry.__table_args__ = (
    sa.Index(
        'deduplication_index',
        Photometry.obj_id,
        Photometry.instrument_id,
        Photometry.origin,
        Photometry.mjd,
        Photometry.fluxerr,
        Photometry.flux,
        unique=True,
    ),
)


User.photometry = relationship(
    'Photometry',
    doc='Photometry uploaded by this User.',
    back_populates='owner',
    foreign_keys="Photometry.owner_id",
)

GroupPhotometry = join_model("group_photometry", Group, Photometry)
GroupPhotometry.__doc__ = "Join table mapping Groups to Photometry."
GroupPhotometry.delete = GroupPhotometry.update = (
    accessible_by_group_admins & GroupPhotometry.read
)


class Spectrum(Base):
    """Wavelength-dependent measurement of the flux of an object through a
    dispersive element."""

    read = accessible_by_groups_members
    update = delete = accessible_by_owner

    __tablename__ = 'spectra'
    # TODO better numpy integration
    wavelengths = sa.Column(
        NumpyArray, nullable=False, doc="Wavelengths of the spectrum [Angstrom]."
    )
    fluxes = sa.Column(
        NumpyArray,
        nullable=False,
        doc="Flux of the Spectrum [F_lambda, arbitrary units].",
    )
    errors = sa.Column(
        NumpyArray,
        doc="Errors on the fluxes of the spectrum [F_lambda, same units as `fluxes`.]",
    )

    obj_id = sa.Column(
        sa.ForeignKey('objs.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="ID of this Spectrum's Obj.",
    )
    obj = relationship('Obj', back_populates='spectra', doc="The Spectrum's Obj.")
    observed_at = sa.Column(
        sa.DateTime,
        nullable=False,
        doc="Median UTC ISO time stamp of the exposure or exposures in which the Spectrum was acquired.",
    )
    origin = sa.Column(sa.String, nullable=True, doc="Origin of the spectrum.")
    # TODO program?
    instrument_id = sa.Column(
        sa.ForeignKey('instruments.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="ID of the Instrument that acquired the Spectrum.",
    )
    instrument = relationship(
        'Instrument',
        back_populates='spectra',
        doc="The Instrument that acquired the Spectrum.",
    )
    groups = relationship(
        "Group",
        secondary="group_spectra",
        back_populates="spectra",
        cascade="save-update, merge, refresh-expire, expunge",
        passive_deletes=True,
        doc='Groups that can view this spectrum.',
    )

    reducers = relationship(
        "User",
        secondary="spectrum_reducers",
        doc="Users that reduced this spectrum.",
    )
    observers = relationship(
        "User",
        secondary="spectrum_observers",
        doc="Users that observed this spectrum.",
    )

    followup_request_id = sa.Column(
        sa.ForeignKey('followuprequests.id', ondelete='SET NULL'), nullable=True
    )
    followup_request = relationship('FollowupRequest', back_populates='spectra')

    assignment_id = sa.Column(
        sa.ForeignKey('classicalassignments.id', ondelete='SET NULL'), nullable=True
    )
    assignment = relationship('ClassicalAssignment', back_populates='spectra')

    altdata = sa.Column(
        psql.JSONB, doc="Miscellaneous alternative metadata.", nullable=True
    )

    original_file_string = sa.Column(
        sa.String,
        doc="Content of original file that was passed to upload the spectrum.",
    )
    original_file_filename = sa.Column(
        sa.String, doc="Original file name that was passed to upload the spectrum."
    )

    owner_id = sa.Column(
        sa.ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="ID of the User who uploaded the spectrum.",
    )
    owner = relationship(
        'User',
        back_populates='spectra',
        foreign_keys=[owner_id],
        cascade='save-update, merge, refresh-expire, expunge',
        doc="The User who uploaded the spectrum.",
    )

    @classmethod
    def from_ascii(
        cls,
        file,
        obj_id=None,
        instrument_id=None,
        observed_at=None,
        wave_column=0,
        flux_column=1,
        fluxerr_column=None,
    ):
        """Generate a `Spectrum` from an ascii file.

        Parameters
        ----------
        file : str or file-like object
           Name or handle of the ASCII file containing the spectrum.
        obj_id : str
           The id of the Obj that this Spectrum is of, if not present
           in the ASCII header.
        instrument_id : int
           ID of the Instrument with which this Spectrum was acquired,
           if not present in the ASCII header.
        observed_at : string or datetime
           Median UTC ISO time stamp of the exposure or exposures in which
           the Spectrum was acquired, if not present in the ASCII header.
        wave_column: integer, optional
           The 0-based index of the ASCII column corresponding to the wavelength
           values of the spectrum (default 0).
        flux_column: integer, optional
           The 0-based index of the ASCII column corresponding to the flux
           values of the spectrum (default 1).
        fluxerr_column: integer or None, optional
           The 0-based index of the ASCII column corresponding to the flux error
           values of the spectrum (default None).
        Returns
        -------
        spec : `skyportal.models.Spectrum`
           The Spectrum generated from the ASCII file.

        """

        try:
            f = open(file, 'rb')  # read as ascii
        except TypeError:
            # it's already a stream
            f = file

        try:
            table = ascii.read(f, comment='#', header_start=None)
        except Exception as e:
            e.args = (f'Error parsing ASCII file: {e.args[0]}',)
            raise
        finally:
            f.close()

        tabledata = np.asarray(table)
        colnames = table.colnames

        # validate the table and some of the input parameters

        # require at least 2 columns (wavelength, flux)
        ncol = len(colnames)
        if ncol < 2:
            raise ValueError(
                'Input data must have at least 2 columns (wavelength, '
                'flux, and optionally flux error).'
            )

        spec_data = {}
        # validate the column indices
        for index, name, dbcol in zip(
            [wave_column, flux_column, fluxerr_column],
            ['wave_column', 'flux_column', 'fluxerr_column'],
            ['wavelengths', 'fluxes', 'errors'],
        ):

            # index format / type validation:
            if dbcol in ['wavelengths', 'fluxes']:
                if not isinstance(index, int):
                    raise ValueError(f'{name} must be an int')
            else:
                if index is not None and not isinstance(index, int):
                    # The only other allowed value is that fluxerr_column can be
                    # None. If the value of index is not None, raise.
                    raise ValueError(f'invalid type for {name}')

            # after validating the indices, ensure that the columns they
            # point to exist
            if isinstance(index, int):
                if index >= ncol:
                    raise ValueError(
                        f'index {name} ({index}) is greater than the '
                        f'maximum allowed value ({ncol - 1})'
                    )
                spec_data[dbcol] = tabledata[colnames[index]].astype(float)

        # parse the header
        if 'comments' in table.meta:

            # this section matches lines like:
            # XTENSION: IMAGE
            # BITPIX: -32
            # NAXIS: 2
            # NAXIS1: 433
            # NAXIS2: 1

            header = {}
            for line in table.meta['comments']:
                try:
                    result = yaml.load(line, Loader=yaml.FullLoader)
                except yaml.YAMLError:
                    continue
                if isinstance(result, dict):
                    header.update(result)

            # this section matches lines like:
            # FILTER  = 'clear   '           / Filter
            # EXPTIME =              600.003 / Total exposure time (sec); avg. of R&B
            # OBJECT  = 'ZTF20abpuxna'       / User-specified object name
            # TARGNAME= 'ZTF20abpuxna_S1'    / Target name (from starlist)
            # DICHNAME= '560     '           / Dichroic

            cards = []
            with warnings.catch_warnings():
                warnings.simplefilter('error', AstropyWarning)
                for line in table.meta['comments']:
                    # this line does not raise a warning
                    card = fits.Card.fromstring(line)
                    try:
                        # this line warns (exception in this context)
                        card.verify()
                    except AstropyWarning:
                        continue
                    cards.append(card)

            # this ensures lines like COMMENT and HISTORY are properly dealt
            # with by using the astropy.header machinery to coerce them to
            # single strings

            fits_header = fits.Header(cards=cards)
            serialized = dict(fits_header)

            commentary_keywords = ['', 'COMMENT', 'HISTORY', 'END']

            for key in serialized:
                # coerce things to serializable JSON
                if key in commentary_keywords:
                    # serialize as a string - otherwise it returns a
                    # funky astropy type that is not json serializable
                    serialized[key] = str(serialized[key])

                if len(fits_header.comments[key]) > 0:
                    header[key] = {
                        'value': serialized[key],
                        'comment': fits_header.comments[key],
                    }
                else:
                    header[key] = serialized[key]

            # this ensures that the spectra are properly serialized to the
            # database JSONB (database JSONB cant handle datetime/date values)
            header = json.loads(to_json(header))

        else:
            header = None

        return cls(
            obj_id=obj_id,
            instrument_id=instrument_id,
            observed_at=observed_at,
            altdata=header,
            **spec_data,
        )


Spectrum.is_modifiable_by = is_modifiable_by
User.spectra = relationship(
    'Spectrum', doc='Spectra uploaded by this User.', back_populates='owner'
)

SpectrumReducer = join_model("spectrum_reducers", Spectrum, User)
SpectrumObserver = join_model("spectrum_observers", Spectrum, User)
SpectrumReducer.create = (
    SpectrumReducer.delete
) = SpectrumReducer.update = AccessibleIfUserMatches('spectrum.owner')
SpectrumObserver.create = (
    SpectrumObserver.delete
) = SpectrumObserver.update = AccessibleIfUserMatches('spectrum.owner')

# should be accessible only by spectrumowner ^^

GroupSpectrum = join_model("group_spectra", Group, Spectrum)
GroupSpectrum.__doc__ = 'Join table mapping Groups to Spectra.'
GroupSpectrum.update = GroupSpectrum.delete = (
    accessible_by_group_admins & GroupSpectrum.read
)


# def format_public_url(context):
#    """TODO migrate this to broker tools"""
#    file_uri = context.current_parameters.get('file_uri')
#    if file_uri is None:
#        return None
#    elif file_uri.startswith('s3'):  # TODO is this reliable?
#        raise NotImplementedError
#    elif file_uri.startswith('http://'): # TODO is this reliable?
#        return file_uri
#    else:  # local file
#        return '/' + file_uri.lstrip('./')


class FollowupRequest(Base):
    """A request for follow-up data (spectroscopy, photometry, or both) using a
    robotic instrument."""

    # TODO: Make read-accessible via target groups
    create = read = AccessibleIfRelatedRowsAreAccessible(obj="read", allocation="read")
    update = delete = (
        AccessibleIfUserMatches('allocation.group.users')
        | AccessibleIfUserMatches('requester')
    ) & read

    requester_id = sa.Column(
        sa.ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
        index=True,
        doc="ID of the User who requested the follow-up.",
    )

    requester = relationship(
        User,
        back_populates='followup_requests',
        doc="The User who requested the follow-up.",
        foreign_keys=[requester_id],
    )

    last_modified_by_id = sa.Column(
        sa.ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
        doc="The ID of the User who last modified the request.",
    )

    last_modified_by = relationship(
        User,
        doc="The user who last modified the request.",
        foreign_keys=[last_modified_by_id],
    )

    obj = relationship('Obj', back_populates='followup_requests', doc="The target Obj.")
    obj_id = sa.Column(
        sa.ForeignKey('objs.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="ID of the target Obj.",
    )

    payload = sa.Column(
        psql.JSONB, nullable=False, doc="Content of the followup request."
    )

    status = sa.Column(
        sa.String(),
        nullable=False,
        default="pending submission",
        index=True,
        doc="The status of the request.",
    )

    allocation_id = sa.Column(
        sa.ForeignKey('allocations.id', ondelete='CASCADE'), nullable=False, index=True
    )
    allocation = relationship('Allocation', back_populates='requests')

    transactions = relationship(
        'FacilityTransaction',
        back_populates='followup_request',
        passive_deletes=True,
        order_by="FacilityTransaction.created_at.desc()",
    )

    target_groups = relationship(
        'Group',
        secondary='request_groups',
        passive_deletes=True,
        doc='Groups to share the resulting data from this request with.',
    )

    photometry = relationship('Photometry', back_populates='followup_request')
    spectra = relationship('Spectrum', back_populates='followup_request')

    @property
    def instrument(self):
        return self.allocation.instrument

    def is_readable_by(self, user_or_token):
        """Return a boolean indicating whether a FollowupRequest belongs to
        an allocation that is accessible to the given user or token.

        Parameters
        ----------
        user_or_token: `baselayer.app.models.User` or `baselayer.app.models.Token`
           The User or Token to check.

        Returns
        -------
        readable: bool
           Whether the FollowupRequest belongs to an Allocation that is
           accessible to the given user or token.
        """

        user_or_token_group_ids = [g.id for g in user_or_token.accessible_groups]
        return self.allocation.group_id in user_or_token_group_ids


FollowupRequestTargetGroup = join_model('request_groups', FollowupRequest, Group)
FollowupRequestTargetGroup.create = (
    FollowupRequestTargetGroup.update
) = FollowupRequestTargetGroup.delete = (
    AccessibleIfUserMatches('followuprequest.requester')
    & FollowupRequestTargetGroup.read
)


class FacilityTransaction(Base):

    created_at = sa.Column(
        sa.DateTime,
        nullable=False,
        default=datetime.utcnow,
        index=True,
        doc="UTC time this FacilityTransaction was created.",
    )

    request = sa.Column(psql.JSONB, doc='Serialized HTTP request.')
    response = sa.Column(psql.JSONB, doc='Serialized HTTP response.')

    followup_request_id = sa.Column(
        sa.ForeignKey('followuprequests.id', ondelete='SET NULL'),
        index=True,
        nullable=True,
        doc="The ID of the FollowupRequest this message pertains to.",
    )

    followup_request = relationship(
        'FollowupRequest',
        back_populates='transactions',
        doc="The FollowupRequest this message pertains to.",
    )

    initiator_id = sa.Column(
        sa.ForeignKey('users.id', ondelete='SET NULL'),
        index=True,
        nullable=True,
        doc='The ID of the User who initiated the transaction.',
    )
    initiator = relationship(
        'User',
        back_populates='transactions',
        doc='The User who initiated the transaction.',
        foreign_keys="FacilityTransaction.initiator_id",
    )


User.followup_requests = relationship(
    'FollowupRequest',
    back_populates='requester',
    passive_deletes=True,
    doc="The follow-up requests this User has made.",
    foreign_keys=[FollowupRequest.requester_id],
)

User.transactions = relationship(
    'FacilityTransaction',
    back_populates='initiator',
    doc="The FacilityTransactions initiated by this User.",
    foreign_keys="FacilityTransaction.initiator_id",
)


class Listing(Base):

    user_id = sa.Column(
        sa.ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="The ID of the User who created this Listing.",
    )

    user = relationship(
        "User",
        foreign_keys=user_id,
        back_populates="listings",
        doc="The user that saved this object/listing",
    )

    obj_id = sa.Column(
        sa.ForeignKey('objs.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="The ID of the object that is on this Listing",
    )

    obj = relationship(
        "Obj",
        doc="The object referenced by this listing",
    )

    list_name = sa.Column(
        sa.String,
        index=True,
        nullable=False,
        doc="Name of the list, e.g., 'favorites'. ",
    )


Listing.__table_args__ = (
    sa.Index(
        "listings_main_index",
        Listing.user_id,
        Listing.obj_id,
        Listing.list_name,
        unique=True,
    ),
    sa.Index(
        "listings_reverse_index",
        Listing.list_name,
        Listing.obj_id,
        Listing.user_id,
        unique=True,
    ),
)


class Thumbnail(Base):
    """Thumbnail image centered on the location of an Obj."""

    create = read = AccessibleIfRelatedRowsAreAccessible(obj='read')

    # TODO delete file after deleting row
    type = sa.Column(
        thumbnail_types, doc='Thumbnail type (e.g., ref, new, sub, dr8, ps1, ...)'
    )
    file_uri = sa.Column(
        sa.String(),
        nullable=True,
        index=False,
        unique=False,
        doc="Path of the Thumbnail on the machine running SkyPortal.",
    )
    public_url = sa.Column(
        sa.String(),
        nullable=True,
        index=False,
        unique=False,
        doc="Publically accessible URL of the thumbnail.",
    )
    origin = sa.Column(sa.String, nullable=True, doc="Origin of the Thumbnail.")
    obj = relationship(
        'Obj',
        back_populates='thumbnails',
        uselist=False,
        doc="The Thumbnail's Obj.",
    )
    obj_id = sa.Column(
        sa.ForeignKey('objs.id', ondelete='CASCADE'),
        index=True,
        nullable=False,
        doc="ID of the thumbnail's obj.",
    )


class ObservingRun(Base):
    """A classical observing run with a target list (of Objs)."""

    update = delete = accessible_by_owner

    instrument_id = sa.Column(
        sa.ForeignKey('instruments.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="ID of the Instrument used for this run.",
    )
    instrument = relationship(
        'Instrument',
        cascade='save-update, merge, refresh-expire, expunge',
        uselist=False,
        back_populates='observing_runs',
        doc="The Instrument for this run.",
    )

    # name of the PI
    pi = sa.Column(sa.String, doc="The name(s) of the PI(s) of this run.")
    observers = sa.Column(sa.String, doc="The name(s) of the observer(s) on this run.")

    sources = relationship(
        'Obj',
        secondary='join(ClassicalAssignment, Obj)',
        cascade='save-update, merge, refresh-expire, expunge',
        passive_deletes=True,
        doc="The targets [Objs] for this run.",
    )

    # let this be nullable to accommodate external groups' runs
    group = relationship(
        'Group',
        back_populates='observing_runs',
        doc='The Group associated with this Run.',
    )
    group_id = sa.Column(
        sa.ForeignKey('groups.id', ondelete='CASCADE'),
        nullable=True,
        index=True,
        doc='The ID of the Group associated with this run.',
    )

    # the person who uploaded the run
    owner = relationship(
        'User',
        back_populates='observing_runs',
        doc="The User who created this ObservingRun.",
        foreign_keys="ObservingRun.owner_id",
    )
    owner_id = sa.Column(
        sa.ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="The ID of the User who created this ObservingRun.",
    )

    assignments = relationship(
        'ClassicalAssignment',
        passive_deletes=True,
        doc="The Target Assignments for this Run.",
    )
    calendar_date = sa.Column(
        sa.Date, nullable=False, index=True, doc="The Local Calendar date of this Run."
    )

    @property
    def calendar_noon(self):
        observer = self.instrument.telescope.observer
        year = self.calendar_date.year
        month = self.calendar_date.month
        day = self.calendar_date.day
        hour = 12
        noon = datetime(
            year=year, month=month, day=day, hour=hour, tzinfo=observer.timezone
        )
        noon = noon.astimezone(timezone.utc).timestamp()
        noon = ap_time.Time(noon, format='unix')
        return noon

    def rise_time(self, target_or_targets, altitude=30 * u.degree):
        """The rise time of the specified targets as an astropy.time.Time."""
        observer = self.instrument.telescope.observer
        sunset = self.instrument.telescope.next_sunset(self.calendar_noon).reshape((1,))
        sunrise = self.instrument.telescope.next_sunrise(self.calendar_noon).reshape(
            (1,)
        )
        original_shape = np.asarray(target_or_targets).shape
        target_array = (
            [target_or_targets] if len(original_shape) == 0 else target_or_targets
        )

        next_rise = observer.target_rise_time(
            sunset, target_array, which='next', horizon=altitude
        ).reshape((len(target_array),))

        # if next rise time is after next sunrise, the target rises before
        # sunset. show the previous rise so that the target is shown to be
        # "already up" when the run begins (a beginning of night target).

        recalc = next_rise > sunrise
        if recalc.any():
            target_subarr = [t for t, b in zip(target_array, recalc) if b]
            next_rise[recalc] = observer.target_rise_time(
                sunset, target_subarr, which='previous', horizon=altitude
            ).reshape((len(target_subarr),))

        return next_rise.reshape(original_shape)

    def set_time(self, target_or_targets, altitude=30 * u.degree):
        """The set time of the specified targets as an astropy.time.Time."""
        observer = self.instrument.telescope.observer
        sunset = self.instrument.telescope.next_sunset(self.calendar_noon)
        original_shape = np.asarray(target_or_targets).shape
        return observer.target_set_time(
            sunset, target_or_targets, which='next', horizon=altitude
        ).reshape(original_shape)


User.observing_runs = relationship(
    'ObservingRun',
    cascade='save-update, merge, refresh-expire, expunge',
    passive_deletes=True,
    doc="Observing Runs this User has created.",
    foreign_keys="ObservingRun.owner_id",
)


class ClassicalAssignment(Base):
    """Assignment of an Obj to an Observing Run as a target."""

    create = read = update = delete = AccessibleIfRelatedRowsAreAccessible(
        obj='read', run='read'
    )

    requester_id = sa.Column(
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="The ID of the User who created this assignment.",
    )
    requester = relationship(
        "User",
        back_populates="assignments",
        foreign_keys=[requester_id],
        doc="The User who created this assignment.",
    )

    last_modified_by_id = sa.Column(
        sa.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
        index=True,
    )
    last_modified_by = relationship("User", foreign_keys=[last_modified_by_id])

    obj = relationship('Obj', back_populates='assignments', doc='The assigned Obj.')
    obj_id = sa.Column(
        sa.ForeignKey('objs.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc='ID of the assigned Obj.',
    )

    comment = sa.Column(
        sa.String(),
        doc="A comment on the assignment. "
        "Typically a justification for the request, "
        "or instructions for taking the data.",
    )
    status = sa.Column(
        sa.String(),
        nullable=False,
        default="pending",
        doc='Status of the assignment [done, not done, pending].',
    )
    priority = sa.Column(
        followup_priorities,
        nullable=False,
        doc='Priority of the request (1 = lowest, 5 = highest).',
    )
    spectra = relationship(
        "Spectrum",
        back_populates="assignment",
        doc="Spectra produced by the assignment.",
    )
    photometry = relationship(
        "Photometry",
        back_populates="assignment",
        doc="Photometry produced by the assignment.",
    )

    run = relationship(
        'ObservingRun',
        back_populates='assignments',
        doc="The ObservingRun this target was assigned to.",
    )
    run_id = sa.Column(
        sa.ForeignKey('observingruns.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="ID of the ObservingRun this target was assigned to.",
    )

    @hybrid_property
    def instrument(self):
        """The instrument in use on the assigned ObservingRun."""
        return self.run.instrument

    @property
    def rise_time(self):
        """The UTC time at which the object rises on this run."""
        target = self.obj.target
        return self.run.rise_time(target)

    @property
    def set_time(self):
        """The UTC time at which the object sets on this run."""
        target = self.obj.target
        return self.run.set_time(target)


User.assignments = relationship(
    'ClassicalAssignment',
    back_populates='requester',
    passive_deletes=True,
    doc="Objs the User has assigned to ObservingRuns.",
    foreign_keys="ClassicalAssignment.requester_id",
)


class Invitation(Base):

    read = update = delete = AccessibleIfUserMatches('invited_by')

    token = sa.Column(sa.String(), nullable=False, unique=True)
    groups = relationship(
        "Group",
        secondary="group_invitations",
        cascade="save-update, merge, refresh-expire, expunge",
        passive_deletes=True,
    )
    streams = relationship(
        "Stream",
        secondary="stream_invitations",
        cascade="save-update, merge, refresh-expire, expunge",
        passive_deletes=True,
    )
    admin_for_groups = sa.Column(psql.ARRAY(sa.Boolean), nullable=False)
    user_email = sa.Column(EmailType(), nullable=True)
    invited_by = relationship(
        "User",
        secondary="user_invitations",
        cascade="save-update, merge, refresh-expire, expunge",
        passive_deletes=True,
        uselist=False,
    )
    used = sa.Column(sa.Boolean, nullable=False, default=False)


GroupInvitation = join_model('group_invitations', Group, Invitation)
StreamInvitation = join_model('stream_invitations', Stream, Invitation)
UserInvitation = join_model("user_invitations", User, Invitation)


@event.listens_for(Invitation, 'after_insert')
def send_user_invite_email(mapper, connection, target):
    app_base_url = get_app_base_url()
    link_location = f'{app_base_url}/login/google-oauth2/?invite_token={target.token}'
    send_email(
        recipients=[target.user_email],
        subject=cfg["invitations.email_subject"],
        body=(
            f'{cfg["invitations.email_body_preamble"]}<br /><br />'
            f'Please click <a href="{link_location}">here</a> to join.'
        ),
    )


class SourceNotification(Base):

    create = read = AccessibleIfRelatedRowsAreAccessible(source='read')
    update = delete = AccessibleIfUserMatches('sent_by')

    groups = relationship(
        "Group",
        secondary="group_notifications",
        cascade="save-update, merge, refresh-expire, expunge",
        passive_deletes=True,
    )
    sent_by_id = sa.Column(
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="The ID of the User who sent this notification.",
    )
    sent_by = relationship(
        "User",
        back_populates="source_notifications",
        foreign_keys=[sent_by_id],
        doc="The User who sent this notification.",
    )
    source_id = sa.Column(
        sa.ForeignKey("objs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="ID of the target Obj.",
    )
    source = relationship(
        'Obj', back_populates='obj_notifications', doc='The target Obj.'
    )

    additional_notes = sa.Column(sa.String(), nullable=True)
    level = sa.Column(sa.String(), nullable=False)


class UserNotification(Base):

    read = update = delete = AccessibleIfUserMatches('user')

    user_id = sa.Column(
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="ID of the associated User",
    )
    user = relationship(
        "User",
        back_populates="notifications",
        doc="The associated User",
    )
    text = sa.Column(
        sa.String(),
        nullable=False,
        doc="The notification text to display",
    )

    viewed = sa.Column(
        sa.Boolean,
        nullable=False,
        default=False,
        index=True,
        doc="Boolean indicating whether notification has been viewed.",
    )

    url = sa.Column(
        sa.String(),
        nullable=True,
        doc="URL to which to direct upon click, if relevant",
    )


User.notifications = relationship(
    "UserNotification",
    back_populates="user",
    passive_deletes=True,
    doc="Notifications to be displayed on front-end associated with User",
)

GroupSourceNotification = join_model('group_notifications', Group, SourceNotification)
GroupSourceNotification.create = (
    GroupSourceNotification.read
) = accessible_by_group_members
GroupSourceNotification.update = (
    GroupSourceNotification.delete
) = accessible_by_group_admins | AccessibleIfUserMatches('sourcenotification.sent_by')

User.source_notifications = relationship(
    'SourceNotification',
    back_populates='sent_by',
    doc="Source notifications the User has sent out.",
    foreign_keys="SourceNotification.sent_by_id",
)


@event.listens_for(SourceNotification, 'after_insert')
def send_source_notification(mapper, connection, target):
    app_base_url = get_app_base_url()

    link_location = f'{app_base_url}/source/{target.source_id}'
    if target.sent_by.first_name is not None and target.sent_by.last_name is not None:
        sent_by_name = f'{target.sent_by.first_name} {target.sent_by.last_name}'
    else:
        sent_by_name = target.sent_by.username

    group_ids = map(lambda group: group.id, target.groups)
    groups = DBSession().query(Group).filter(Group.id.in_(group_ids)).all()

    target_users = set()
    for group in groups:
        # Use a set to get unique iterable of users
        target_users.update(group.users)

    source = DBSession().query(Obj).get(target.source_id)
    source_info = ""
    if source.ra is not None:
        source_info += f'RA={source.ra} '
    if source.dec is not None:
        source_info += f'Dec={source.dec}'
    source_info = source_info.strip()

    # Send SMS messages to opted-in users if desired
    if target.level == "hard":
        message_text = (
            f'{cfg["app.title"]}: {sent_by_name} would like to call your immediate'
            f' attention to a source at {link_location} ({source_info}).'
        )
        if target.additional_notes != "" and target.additional_notes is not None:
            message_text += f' Addtional notes: {target.additional_notes}'

        account_sid = cfg["twilio.sms_account_sid"]
        auth_token = cfg["twilio.sms_auth_token"]
        from_number = cfg["twilio.from_number"]
        client = TwilioClient(account_sid, auth_token)
        for user in target_users:
            # If user has a phone number registered and opted into SMS notifications
            if (
                user.contact_phone is not None
                and user.preferences is not None
                and "allowSMSAlerts" in user.preferences
                and user.preferences.get("allowSMSAlerts")
            ):
                client.messages.create(
                    body=message_text, from_=from_number, to=user.contact_phone.e164
                )

    # Send email notifications
    recipients = []
    for user in target_users:
        # If user has a contact email registered and opted into email notifications
        if (
            user.contact_email is not None
            and user.preferences is not None
            and "allowEmailAlerts" in user.preferences
            and user.preferences.get("allowEmailAlerts")
        ):
            recipients.append(user.contact_email)

    descriptor = "immediate" if target.level == "hard" else ""
    html_content = (
        f'{sent_by_name} would like to call your {descriptor} attention to'
        f' <a href="{link_location}">{target.source_id}</a> ({source_info})'
    )
    if target.additional_notes != "" and target.additional_notes is not None:
        html_content += f'<br /><br />Additional notes: {target.additional_notes}'

    if len(recipients) > 0:
        send_email(
            recipients=recipients,
            subject=f'{cfg["app.title"]}: Source Alert',
            body=html_content,
        )


@event.listens_for(User, 'after_insert')
def create_single_user_group(mapper, connection, target):

    # Create single-user group
    @event.listens_for(DBSession(), "after_flush", once=True)
    def receive_after_flush(session, context):
        session.add(
            Group(name=slugify(target.username), users=[target], single_user_group=True)
        )


@event.listens_for(User, 'before_delete')
def delete_single_user_group(mapper, connection, target):
    single_user_group = target.single_user_group

    # Delete single-user group
    @event.listens_for(DBSession(), "after_flush_postexec", once=True)
    def receive_after_flush(session, context):
        DBSession().delete(single_user_group)


@event.listens_for(User, 'after_update')
def update_single_user_group(mapper, connection, target):

    # Update single user group name if needed
    @event.listens_for(DBSession(), "after_flush_postexec", once=True)
    def receive_after_flush(session, context):
        single_user_group = target.single_user_group
        single_user_group.name = slugify(target.username)
        DBSession().add(single_user_group)


schema.setup_schema()
