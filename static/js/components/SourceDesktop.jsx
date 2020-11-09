import React, { useState, Suspense } from "react";
import PropTypes from "prop-types";
import { useSelector } from "react-redux";
import { Link } from "react-router-dom";

import { makeStyles } from "@material-ui/core/styles";
import Button from "@material-ui/core/Button";
import Chip from "@material-ui/core/Chip";
import Tooltip from "@material-ui/core/Tooltip";
import Accordion from "@material-ui/core/Accordion";
import AccordionSummary from "@material-ui/core/AccordionSummary";
import AccordionDetails from "@material-ui/core/AccordionDetails";
import ExpandMoreIcon from "@material-ui/icons/ExpandMore";
import Typography from "@material-ui/core/Typography";

import CommentList from "./CommentList";
import ClassificationList from "./ClassificationList";
import ClassificationForm from "./ClassificationForm";
import ShowClassification from "./ShowClassification";
import ThumbnailList from "./ThumbnailList";
import SurveyLinkList from "./SurveyLinkList";
import StarList from "./StarList";
import { ra_to_hours, dec_to_dms } from "../units";
import FollowupRequestForm from "./FollowupRequestForm";
import FollowupRequestLists from "./FollowupRequestLists";
import SharePage from "./SharePage";
import AssignmentForm from "./AssignmentForm";
import AssignmentList from "./AssignmentList";
import SourceNotification from "./SourceNotification";
import EditSourceGroups from "./EditSourceGroups";
import UpdateSourceRedshift from "./UpdateSourceRedshift";
import SourceRedshiftHistory from "./SourceRedshiftHistory";
import ObjPageAnnotations from "./ObjPageAnnotations";
import SourceSaveHistory from "./SourceSaveHistory";

const Plot = React.lazy(() => import(/* webpackChunkName: "Bokeh" */ "./Plot"));

const CentroidPlot = React.lazy(() =>
  import(/* webpackChunkName: "CentroidPlot" */ "./CentroidPlot")
);

// Export to allow Candidate.jsx to use styles
export const useSourceStyles = makeStyles((theme) => ({
  chip: {
    margin: theme.spacing(0.5),
  },
  accordionHeading: {
    fontSize: "1.25rem",
    fontWeight: theme.typography.fontWeightRegular,
  },
  photometryContainer: {
    display: "flex",
    overflowX: "scroll",
    flexDirection: "column",
    padding: "0.5rem",
    "& div button": {
      margin: "0.5rem",
    },
  },
  source: {
    padding: "1rem",
    display: "flex",
    flexDirection: "row",
  },
  leftColumn: {
    display: "flex",
    flexFlow: "column nowrap",
    verticalAlign: "top",
    paddingRight: "2rem",
    flex: "0 2 900px",
    minWidth: 0,
  },
  columnItem: {
    margin: "0.5rem 0",
  },
  rightColumn: {
    display: "flex",
    flex: "0 1 20em",
    verticalAlign: "top",
    flexFlow: "column nowrap",
  },
  name: {
    fontSize: "200%",
    fontWeight: "900",
    color: "darkgray",
    paddingBottom: "0.25em",
    display: "inline-block",
  },
  smallPlot: {
    width: "350px",
    overflow: "auto",
  },
  comments: {},
  classifications: {
    display: "flex",
    flexDirection: "column",
  },
  alignRight: {
    display: "inline-block",
    verticalAlign: "super",
  },
  followupContainer: {
    display: "flex",
    flexDirection: "column",
  },
  position: {
    fontWeight: "bold",
    fontSize: "110%",
  },
}));

const SourceDesktop = ({ source }) => {
  const classes = useSourceStyles();

  const [showStarList, setShowStarList] = useState(false);

  const { instrumentList, instrumentFormParams } = useSelector(
    (state) => state.instruments
  );
  const { observingRunList } = useSelector((state) => state.observingRuns);
  const { taxonomyList } = useSelector((state) => state.taxonomies);
  const userAccessibleGroups = useSelector(
    (state) => state.groups.userAccessible
  );

  return (
    <div className={classes.source}>
      <div className={classes.leftColumn}>
        <div className={classes.leftColumnItem}>
          <div className={classes.alignRight}>
            <SharePage />
          </div>
          <div className={classes.name}>{source.id}</div>
          <br />
          <ShowClassification
            classifications={source.classifications}
            taxonomyList={taxonomyList}
          />
          <b>Position (J2000):</b>
          &nbsp; &nbsp;
          <span className={classes.position}>
            {ra_to_hours(source.ra)} &nbsp;
            {dec_to_dms(source.dec)}
          </span>
          &nbsp; (&alpha;,&delta;= {source.ra}, &nbsp;
          {source.dec}; <i>l</i>,<i>b</i>={source.gal_lon.toFixed(6)}, &nbsp;
          {source.gal_lat.toFixed(6)}
          )
          <br />
          <>
            <b>Redshift: &nbsp;</b>
            {source.redshift && source.redshift.toFixed(4)}
            <UpdateSourceRedshift source={source} />
            <SourceRedshiftHistory redshiftHistory={source.redshift_history} />
          </>
          {source.dm && (
            <>
              &nbsp;|&nbsp;
              <b>DM: &nbsp;</b>
              {source.dm.toFixed(3)}
              &nbsp; mag
            </>
          )}
          {source.luminosity_distance && (
            <>
              &nbsp;|&nbsp;
              <b>
                <i>D</i>
                <sub>L</sub>: &nbsp;
              </b>
              {source.luminosity_distance.toFixed(2)}
              &nbsp; Mpc
            </>
          )}
          {source.redshift != null && <>&nbsp;|&nbsp;</>}
          Finding Chart:&nbsp;
          <Button
            href={`/api/sources/${source.id}/finder`}
            download="finder-chart-pdf"
          >
            PDF
          </Button>
          <Link to={`/source/${source.id}/finder`} role="link">
            <Button>Interactive</Button>
          </Link>
          &nbsp;|&nbsp;
          <Button onClick={() => setShowStarList(!showStarList)}>
            {showStarList ? "Hide Starlist" : "Show Starlist"}
          </Button>
          <br />
          {showStarList && <StarList sourceId={source.id} />}
          {source.groups.map((group) => (
            <Tooltip
              title={`Saved at ${group.saved_at} by ${group.saved_by?.username}`}
              key={group.id}
            >
              <Chip
                label={
                  group.nickname
                    ? group.nickname.substring(0, 15)
                    : group.name.substring(0, 15)
                }
                size="small"
                className={classes.chip}
                data-testid={`groupChip_${group.id}`}
              />
            </Tooltip>
          ))}
          <EditSourceGroups
            source={{
              id: source.id,
              currentGroupIds: source.groups.map((g) => g.id),
            }}
            userGroups={userAccessibleGroups}
            icon
          />
          <SourceSaveHistory groups={source.groups} />
        </div>
        <div className={classes.columnItem}>
          <ThumbnailList
            ra={source.ra}
            dec={source.dec}
            thumbnails={source.thumbnails}
            size="12.875rem"
          />
        </div>
        <div className={classes.columnItem}>
          <Accordion defaultExpanded>
            <AccordionSummary
              expandIcon={<ExpandMoreIcon />}
              aria-controls="surveys-content"
              id="surveys-header"
            >
              <Typography className={classes.accordionHeading}>
                Surveys
              </Typography>
            </AccordionSummary>
            <AccordionDetails>
              <SurveyLinkList id={source.id} ra={source.ra} dec={source.dec} />
            </AccordionDetails>
          </Accordion>
        </div>
        <div className={classes.columnItem}>
          <Accordion defaultExpanded>
            <AccordionSummary
              expandIcon={<ExpandMoreIcon />}
              aria-controls="photometry-content"
              id="photometry-header"
            >
              <Typography className={classes.accordionHeading}>
                Photometry
              </Typography>
            </AccordionSummary>
            <AccordionDetails>
              <div className={classes.photometryContainer}>
                <Suspense fallback={<div>Loading photometry plot...</div>}>
                  <Plot
                    url={`/api/internal/plot/photometry/${source.id}?width=800&height=500`}
                  />
                </Suspense>
                <div>
                  <Link to={`/upload_photometry/${source.id}`} role="link">
                    <Button variant="contained">
                      Upload additional photometry
                    </Button>
                  </Link>
                  <Link to={`/share_data/${source.id}`} role="link">
                    <Button variant="contained">Share data</Button>
                  </Link>
                </div>
              </div>
            </AccordionDetails>
          </Accordion>
        </div>
        <div className={classes.columnItem}>
          <Accordion defaultExpanded>
            <AccordionSummary
              expandIcon={<ExpandMoreIcon />}
              aria-controls="spectroscopy-content"
              id="spectroscopy-header"
            >
              <Typography className={classes.accordionHeading}>
                Spectroscopy
              </Typography>
            </AccordionSummary>
            <AccordionDetails>
              <div className={classes.photometryContainer}>
                <Suspense fallback={<div>Loading spectroscopy plot...</div>}>
                  <Plot
                    url={`/api/internal/plot/spectroscopy/${source.id}?width=800&height=500`}
                  />
                </Suspense>
                <div>
                  <Link to={`/upload_spectrum/${source.id}`} role="link">
                    <Button variant="contained">
                      Upload additional spectroscopy
                    </Button>
                  </Link>
                  <Link to={`/share_data/${source.id}`} role="link">
                    <Button variant="contained">Share data</Button>
                  </Link>
                </div>
              </div>
            </AccordionDetails>
          </Accordion>
        </div>
        {/* TODO 1) check for dead links; 2) simplify link formatting if possible */}
        <div className={classes.columnItem}>
          <Accordion defaultExpanded>
            <AccordionSummary
              expandIcon={<ExpandMoreIcon />}
              aria-controls="followup-content"
              id="followup-header"
            >
              <Typography className={classes.accordionHeading}>
                Follow-up
              </Typography>
            </AccordionSummary>
            <AccordionDetails>
              <div className={classes.followupContainer}>
                <FollowupRequestForm
                  obj_id={source.id}
                  action="createNew"
                  instrumentList={instrumentList}
                  instrumentFormParams={instrumentFormParams}
                />
                <FollowupRequestLists
                  followupRequests={source.followup_requests}
                  instrumentList={instrumentList}
                  instrumentFormParams={instrumentFormParams}
                />
                <AssignmentForm
                  obj_id={source.id}
                  observingRunList={observingRunList}
                />
                <AssignmentList assignments={source.assignments} />
              </div>
            </AccordionDetails>
          </Accordion>
        </div>
      </div>

      <div className={classes.rightColumn}>
        <div className={classes.columnItem}>
          <Accordion defaultExpanded>
            <AccordionSummary
              expandIcon={<ExpandMoreIcon />}
              aria-controls="annotations-content"
              id="annotations-header"
            >
              <Typography className={classes.accordionHeading}>
                Auto-annotations
              </Typography>
            </AccordionSummary>
            <AccordionDetails>
              <ObjPageAnnotations annotations={source.annotations} />
            </AccordionDetails>
          </Accordion>
        </div>
        <div className={classes.columnItem}>
          <Accordion defaultExpanded className={classes.comments}>
            <AccordionSummary
              expandIcon={<ExpandMoreIcon />}
              aria-controls="comments-content"
              id="comments-header"
            >
              <Typography className={classes.accordionHeading}>
                Comments
              </Typography>
            </AccordionSummary>
            <AccordionDetails>
              <CommentList />
            </AccordionDetails>
          </Accordion>
        </div>
        <div className={classes.columnItem}>
          <Accordion defaultExpanded className={classes.classifications}>
            <AccordionSummary
              expandIcon={<ExpandMoreIcon />}
              aria-controls="classifications-content"
              id="classifications-header"
            >
              <Typography className={classes.accordionHeading}>
                Classifications
              </Typography>
            </AccordionSummary>
            <AccordionDetails>
              <div className={classes.classifications}>
                <ClassificationList />
                <ClassificationForm
                  obj_id={source.id}
                  action="createNew"
                  taxonomyList={taxonomyList}
                />
              </div>
            </AccordionDetails>
          </Accordion>
        </div>
        <div className={classes.columnItem}>
          <Accordion defaultExpanded>
            <AccordionSummary
              expandIcon={<ExpandMoreIcon />}
              aria-controls="centroidplot-content"
              id="centroidplot-header"
            >
              <Typography className={classes.accordionHeading}>
                Centroid Plot
              </Typography>
            </AccordionSummary>
            <AccordionDetails>
              <Suspense fallback={<div>Loading centroid plot...</div>}>
                <CentroidPlot
                  className={classes.smallPlot}
                  sourceId={source.id}
                  size="21.875rem"
                />
              </Suspense>
            </AccordionDetails>
          </Accordion>
        </div>
        <div className={classes.columnItem}>
          <Accordion defaultExpanded>
            <AccordionSummary
              expandIcon={<ExpandMoreIcon />}
              aria-controls="notifications-content"
              id="notifications-header"
            >
              <Typography className={classes.accordionHeading}>
                Source Notification
              </Typography>
            </AccordionSummary>
            <AccordionDetails>
              <SourceNotification sourceId={source.id} />
            </AccordionDetails>
          </Accordion>
        </div>
      </div>
    </div>
  );
};

SourceDesktop.propTypes = {
  source: PropTypes.shape({
    id: PropTypes.string.isRequired,
    ra: PropTypes.number,
    dec: PropTypes.number,
    loadError: PropTypes.oneOfType([PropTypes.string, PropTypes.bool]),
    thumbnails: PropTypes.arrayOf(PropTypes.shape({})),
    redshift: PropTypes.number,
    groups: PropTypes.arrayOf(PropTypes.shape({})),
    gal_lon: PropTypes.number,
    gal_lat: PropTypes.number,
    dm: PropTypes.number,
    luminosity_distance: PropTypes.number,
    annotations: PropTypes.arrayOf(
      PropTypes.shape({
        origin: PropTypes.string.isRequired,
        data: PropTypes.object.isRequired, // eslint-disable-line react/forbid-prop-types
      })
    ),
    classifications: PropTypes.arrayOf(
      PropTypes.shape({
        author_name: PropTypes.string,
        probability: PropTypes.number,
        modified: PropTypes.string,
        classification: PropTypes.string,
        id: PropTypes.number,
        obj_id: PropTypes.string,
        author_id: PropTypes.number,
        taxonomy_id: PropTypes.number,
        created_at: PropTypes.string,
      })
    ),
    followup_requests: PropTypes.arrayOf(PropTypes.any),
    assignments: PropTypes.arrayOf(PropTypes.any),
    redshift_history: PropTypes.arrayOf(PropTypes.any),
  }).isRequired,
};

export default SourceDesktop;
