import React, { useEffect } from "react";
import { useSelector, useDispatch } from "react-redux";
import PropTypes from "prop-types";
import { CountUp } from "use-count-up";

import { makeStyles } from "@material-ui/core/styles";
import Paper from "@material-ui/core/Paper";
import Typography from "@material-ui/core/Typography";
import DragHandleIcon from "@material-ui/icons/DragHandle";

import WidgetPrefsDialog from "./WidgetPrefsDialog";
import * as profileActions from "../ducks/profile";

const useStyles = makeStyles(() => ({
  counter: {
    display: "flex",
    flexDirection: "column",
    height: "100%",
    justifyContent: "center",
    marginRight: "2rem",
  },
  widgetsBar: {
    position: "fixed",
    right: "1rem",
    zIndex: 1,
  },
}));

const SourceCounts = ({ classes, sinceDaysAgo }) => {
  const styles = useStyles();
  const sourceCounts = useSelector((state) => state.sourceCounts.sourceCounts);
  const userPrefs = useSelector(
    (state) => state.profile.preferences.sourceCounts
  );
  const dispatch = useDispatch();

  const defaultPrefs = {
    sinceDaysAgo: sinceDaysAgo ? parseInt(sinceDaysAgo, 10) : "",
  };
  const sourceCountPrefs = userPrefs || defaultPrefs;

  useEffect(() => {
    // If user prefs is blank, update to app default
    if (!userPrefs) {
      dispatch(profileActions.updateUserPreferences(sourceCountPrefs));
    }
  });

  return (
    <Paper
      id="sourceCountsWidget"
      elevation={1}
      className={classes.widgetPaperFillSpace}
    >
      <div className={classes.widgetPaperDiv}>
        <div className={styles.widgetsBar}>
          <DragHandleIcon className={`${classes.widgetIcon} dragHandle`} />
          <div className={classes.widgetIcon}>
            <WidgetPrefsDialog
              formValues={sourceCountPrefs}
              stateBranchName="sourceCounts"
              title="Source Count Preferences"
              onSubmit={profileActions.updateUserPreferences}
            />
          </div>
        </div>
        <div className={styles.counter}>
          <Typography align="center" variant="h4">
            <b>
              <CountUp
                id="sourceCounter"
                isCounting
                end={sourceCounts?.count}
                duration={1.0}
              />
            </b>
          </Typography>
          <Typography align="center" variant="body1">
            New Sources <br />
            <i>Last {sourceCounts?.since_days_ago} days</i>
          </Typography>
        </div>
      </div>
    </Paper>
  );
};

SourceCounts.propTypes = {
  classes: PropTypes.shape({
    widgetPaperDiv: PropTypes.string.isRequired,
    widgetIcon: PropTypes.string.isRequired,
    widgetPaperFillSpace: PropTypes.string.isRequired,
  }).isRequired,
  sinceDaysAgo: PropTypes.number,
};

SourceCounts.defaultProps = {
  sinceDaysAgo: undefined,
};
export default SourceCounts;
