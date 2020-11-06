import React, { useEffect, Suspense } from "react";
import PropTypes from "prop-types";
import { useSelector, useDispatch } from "react-redux";

import Accordion from "@material-ui/core/Accordion";
import AccordionSummary from "@material-ui/core/AccordionSummary";
import AccordionDetails from "@material-ui/core/AccordionDetails";
import ExpandMoreIcon from "@material-ui/icons/ExpandMore";
import Typography from "@material-ui/core/Typography";
import Paper from "@material-ui/core/Paper";
import { makeStyles } from "@material-ui/core/styles";
import CommentList from "./CommentList";
import ThumbnailList from "./ThumbnailList";
import SurveyLinkList from "./SurveyLinkList";
import SharePage from "./SharePage";
import { useSourceStyles } from "./SourceMobile";
import ObjPageAnnotations from "./ObjPageAnnotations";
import { ra_to_hours, dec_to_dms } from "../units";

import fetchCandidate from "../ducks/candidate";

const Plot = React.lazy(() => import(/* webpackChunkName: "Bokeh" */ "./Plot"));

export const useStyles = makeStyles(() => ({
  topRow: {
    display: "flex",
    flexFlow: "row wrap",
    justifyContent: "center",
  },
  comments: {
    marginLeft: "1rem",
    padding: "1rem",
  },
  accordionItem: {
    maxWidth: "900px",
  },
}));

const Candidate = ({ route }) => {
  const classes = useSourceStyles();
  // Override some default styles from Source component
  const candidateStyles = useStyles();

  const dispatch = useDispatch();
  const candidate = useSelector((state) => state.candidate);
  const cachedCandidateId = candidate ? candidate.id : null;
  const isCached = route.id === cachedCandidateId;

  useEffect(() => {
    const retrieveCandidate = () => {
      dispatch(fetchCandidate(route.id));
    };

    if (!isCached) {
      retrieveCandidate();
    }
  }, [dispatch, isCached, route.id]);

  if (candidate.loadError) {
    return <div>{candidate.loadError}</div>;
  }
  if (!isCached) {
    return (
      <div>
        <span>Loading...</span>
      </div>
    );
  }
  if (candidate.id === undefined) {
    return <div>Candidate not found</div>;
  }

  return (
    <Paper elevation={1}>
      <div className={classes.source}>
        <div className={classes.mainColumn}>
          <div className={candidateStyles.topRow}>
            <div className={classes.column}>
              <div>
                <div className={classes.alignRight}>
                  <SharePage />
                </div>
                <div className={classes.name}>{candidate.id}</div>
              </div>
              <div>
                <b>Position (J2000):</b>
                &nbsp; &nbsp;
                <span className={classes.position}>
                  {ra_to_hours(candidate.ra)} &nbsp;
                  {dec_to_dms(candidate.dec)}
                </span>
                &nbsp; (&alpha;,&delta;= {candidate.ra}, &nbsp;
                {candidate.dec}; <i>l</i>,<i>b</i>=
                {candidate.gal_lon.toFixed(6)}, &nbsp;
                {candidate.gal_lat.toFixed(6)}
                )
                <br />
                {candidate.redshift != null && (
                  <>
                    <b>Redshift: &nbsp;</b>
                    {candidate.redshift?.toFixed(4)}
                  </>
                )}
                {candidate.dm && (
                  <>
                    &nbsp;|&nbsp;
                    <b>DM: &nbsp;</b>
                    {candidate.dm.toFixed(3)}
                    &nbsp; mag
                  </>
                )}
                {candidate.luminosity_distance && (
                  <>
                    &nbsp;|&nbsp;
                    <b>
                      <i>D</i>
                      <sub>L</sub>: &nbsp;
                    </b>
                    {candidate.luminosity_distance.toFixed(2)}
                    &nbsp; Mpc
                  </>
                )}
              </div>
              <ThumbnailList
                ra={candidate.ra}
                dec={candidate.dec}
                thumbnails={candidate.thumbnails}
              />
            </div>
            <Paper className={candidateStyles.comments} variant="outlined">
              <Typography className={classes.accordionHeading}>
                Comments
              </Typography>
              <CommentList isCandidate />
            </Paper>
          </div>
          <div className={candidateStyles.accordionItem}>
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
                <ObjPageAnnotations annotations={candidate.annotations} />
              </AccordionDetails>
            </Accordion>
          </div>
          <div className={candidateStyles.accordionItem}>
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
                      className={classes.plot}
                      url={`/api/internal/plot/photometry/${candidate.id}`}
                    />
                  </Suspense>
                </div>
              </AccordionDetails>
            </Accordion>
          </div>
          <div className={candidateStyles.accordionItem}>
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
                      className={classes.plot}
                      url={`/api/internal/plot/spectroscopy/${candidate.id}`}
                    />
                  </Suspense>
                </div>
              </AccordionDetails>
            </Accordion>
          </div>
          {/* TODO 1) check for dead links; 2) simplify link formatting if possible */}
          <div className={candidateStyles.accordionItem}>
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
                <SurveyLinkList
                  id={candidate.id}
                  ra={candidate.ra}
                  dec={candidate.dec}
                />
              </AccordionDetails>
            </Accordion>
          </div>
        </div>
      </div>
    </Paper>
  );
};

Candidate.propTypes = {
  route: PropTypes.shape({
    id: PropTypes.string,
  }).isRequired,
};

export default Candidate;
