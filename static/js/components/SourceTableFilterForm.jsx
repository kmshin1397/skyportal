import React, { useState } from "react";
import PropTypes from "prop-types";
import { useSelector } from "react-redux";

import Paper from "@material-ui/core/Paper";
import Button from "@material-ui/core/Button";
import ButtonGroup from "@material-ui/core/ButtonGroup";
import Checkbox from "@material-ui/core/Checkbox";
import FormControlLabel from "@material-ui/core/FormControlLabel";
import TextField from "@material-ui/core/TextField";
import { makeStyles, useTheme } from "@material-ui/core/styles";
import Typography from "@material-ui/core/Typography";
import Input from "@material-ui/core/Input";
import Chip from "@material-ui/core/Chip";
import Select from "@material-ui/core/Select";
import MenuItem from "@material-ui/core/MenuItem";
import { useForm, Controller } from "react-hook-form";

import { allowedClasses } from "./ClassificationForm";

const useStyles = makeStyles((theme) => ({
  paperDiv: {
    padding: "1rem",
    height: "100%",
  },
  tableGrid: {
    width: "100%",
  },
  formControl: {
    margin: theme.spacing(1),
    minWidth: 120,
  },
  paper: {
    padding: "1rem",
    marginTop: "1rem",
  },
  root: {
    display: "flex",
    flexWrap: "wrap",
    justifyContent: "space-between",
    "& .MuiTextField-root": {
      margin: theme.spacing(0.2),
      width: "10rem",
    },
  },
  formItem: {
    flex: "1 1 45%",
    margin: "0.5rem",
  },
  formItemRightColumn: {
    flex: "1 1 50%",
    margin: "0.5rem",
  },
  positionField: {
    width: "33%",
  },
  formButtons: {
    width: "100%",
    margin: "0.5rem",
  },
  title: {
    margin: "0.5rem 0rem 0rem 0rem",
  },
  spinner: {
    marginTop: "1rem",
  },
  multiSelect: {
    maxWidth: "100%",
    "& > div": {
      whiteSpace: "normal",
    },
  },
  checkboxGroup: {
    display: "flex",
    flexWrap: "wrap",
    width: "100%",
    "& > label": {
      marginRight: "1rem",
    },
  },
}));

const getMultiselectStyles = (value, selectedValues, theme) => {
  return {
    fontWeight:
      selectedValues.indexOf(value) === -1
        ? theme.typography.fontWeightRegular
        : theme.typography.fontWeightMedium,
  };
};

const SourceTableFilterForm = ({ handleFilterSubmit, groups }) => {
  const classes = useStyles();
  const theme = useTheme();

  const ITEM_HEIGHT = 48;
  const MenuProps = {
    PaperProps: {
      style: {
        maxHeight: ITEM_HEIGHT * 4.5,
      },
    },
  };

  // Get unique classification names, in alphabetical order
  const { taxonomyList } = useSelector((state) => state.taxonomies);
  const latestTaxonomyList = taxonomyList.filter((t) => t.isLatest);
  let classifications = [];
  latestTaxonomyList.forEach((taxonomy) => {
    const currentClasses = allowedClasses(taxonomy.hierarchy).map(
      (option) => `${taxonomy.name}: ${option.class}`
    );
    classifications = classifications.concat(currentClasses);
  });
  classifications = Array.from(new Set(classifications)).sort();

  const [selectedClassifications, setSelectedClassifications] = useState([]);

  const { handleSubmit, register, getValues, control, reset } = useForm();

  const [selectedGroups, setSelectedGroups] = useState([]);
  const groupIDToName = {};
  groups.forEach((g) => {
    groupIDToName[g.id] = g.name;
  });

  const validateGroups = () => {
    const formState = getValues({ nest: true });
    return formState.groupIds.length !== 0;
  };

  const handleClickReset = () => {
    reset();
  };

  return (
    <Paper className={classes.paper} variant="outlined">
      <div>
        <h4> Filter Sources By</h4>
      </div>
      <form
        className={classes.root}
        onSubmit={handleSubmit(handleFilterSubmit)}
      >
        <div className={classes.formItem}>
          <Typography variant="subtitle2" className={classes.title}>
            Name or ID
          </Typography>
          <TextField
            label="Source ID/Name"
            name="sourceID"
            inputRef={register}
          />
        </div>
        <div className={classes.formItemRightColumn}>
          <Typography variant="subtitle2" className={classes.title}>
            Position
          </Typography>
          <TextField
            size="small"
            label="RA (deg)"
            name="ra"
            type="number"
            inputProps={{
              step: 0.001,
            }}
            inputRef={register}
            className={classes.positionField}
          />
          <TextField
            size="small"
            label="Dec (deg)"
            name="dec"
            type="number"
            inputProps={{
              step: 0.001,
            }}
            inputRef={register}
            className={classes.positionField}
          />
          <TextField
            size="small"
            label="Radius (deg)"
            name="radius"
            type="number"
            inputProps={{
              step: 0.001,
            }}
            inputRef={register}
            className={classes.positionField}
          />
        </div>
        <div className={classes.formItem}>
          <Typography variant="subtitle2" className={classes.title}>
            Time Last Detected (UTC)
          </Typography>
          <TextField
            size="small"
            label="Start Date"
            name="startDate"
            inputRef={register}
            placeholder="2012-08-30T00:00:00"
          />
          <TextField
            size="small"
            label="End Date"
            name="endDate"
            inputRef={register}
            placeholder="2012-08-30T00:00:00"
          />
        </div>
        <div className={classes.formItemRightColumn}>
          <Typography variant="subtitle2" className={classes.title}>
            Simbad Class
          </Typography>
          <TextField
            size="small"
            label="Class Name"
            type="text"
            name="simbadClass"
            inputRef={register}
          />
        </div>
        <div className={classes.formItem}>
          <Typography variant="subtitle2" className={classes.title}>
            Classification
          </Typography>
          <Controller
            render={({ onChange, value }) => (
              <Select
                labelId="classifications-select-label"
                id="classifications-select"
                multiple
                value={value}
                onChange={(event) => {
                  setSelectedClassifications(event.target.value);
                  onChange(event.target.value);
                }}
                input={
                  <Input
                    className={classes.multiSelect}
                    id="classifications-select"
                  />
                }
                renderValue={(selected) => (
                  <div className={classes.chips}>
                    {selected.map((classification) => (
                      <Chip
                        key={classification}
                        label={classification}
                        className={classes.chip}
                      />
                    ))}
                  </div>
                )}
                MenuProps={MenuProps}
              >
                {classifications.map((classification) => (
                  <MenuItem
                    key={classification}
                    value={classification}
                    style={getMultiselectStyles(
                      classification,
                      selectedClassifications,
                      theme
                    )}
                  >
                    {classification}
                  </MenuItem>
                ))}
              </Select>
            )}
            name="classifications"
            control={control}
            defaultValue={[]}
          />
        </div>
        <div className={classes.formItemRightColumn}>
          <Typography variant="subtitle2" className={classes.title}>
            Redshift
          </Typography>
          <TextField
            size="small"
            label="Min"
            name="minRedshift"
            type="number"
            inputProps={{
              step: 0.001,
            }}
            inputRef={register}
          />
          <TextField
            size="small"
            label="Max"
            name="maxRedshift"
            type="number"
            inputProps={{
              step: 0.001,
            }}
            inputRef={register}
          />
        </div>
        <div className={classes.formItem}>
          <Typography variant="subtitle2" className={classes.title}>
            Peak Magnitude
          </Typography>
          <TextField
            size="small"
            label="Min"
            name="minPeakMagnitude"
            type="number"
            inputProps={{
              step: 0.001,
            }}
            inputRef={register}
          />
          <TextField
            size="small"
            label="Max"
            name="maxPeakMagnitude"
            type="number"
            inputProps={{
              step: 0.001,
            }}
            inputRef={register}
          />
        </div>
        <div className={classes.formItemRightColumn}>
          <Typography variant="subtitle2" className={classes.title}>
            Latest Magnitude
          </Typography>
          <TextField
            size="small"
            label="Min"
            name="minLatestMagnitude"
            type="number"
            inputProps={{
              step: 0.001,
            }}
            inputRef={register}
          />
          <TextField
            size="small"
            label="Max"
            name="maxLatestMagnitude"
            type="number"
            inputProps={{
              step: 0.001,
            }}
            inputRef={register}
          />
        </div>
        <div className={classes.formItem}>
          <Typography variant="subtitle2" className={classes.title}>
            Which have a...
          </Typography>
          <div className={classes.checkboxGroup}>
            <FormControlLabel
              label="TNS Name"
              labelPlacement="start"
              control={
                <Controller
                  render={({ onChange, value }) => (
                    <Checkbox
                      color="primary"
                      type="checkbox"
                      onChange={(event) => onChange(event.target.checked)}
                      checked={value}
                    />
                  )}
                  name="hasTNSname"
                  control={control}
                  defaultValue={false}
                />
              }
            />
            <FormControlLabel
              label="Spectrum"
              labelPlacement="start"
              control={
                <Controller
                  render={({ onChange, value }) => (
                    <Checkbox
                      color="primary"
                      type="checkbox"
                      onChange={(event) => onChange(event.target.checked)}
                      checked={value}
                    />
                  )}
                  name="hasSpectrum"
                  control={control}
                  defaultValue={false}
                />
              }
            />
          </div>
        </div>
        {groups && (
          <div className={classes.formItemRightColumn}>
            <Typography variant="subtitle2" className={classes.title}>
              Group(s)
            </Typography>
            <Controller
              id="groupSelect"
              name="groupIds"
              labelId="sourcesFilterGroupSelectLabel"
              as={Select}
              control={control}
              rules={{
                required: true,
                validate: validateGroups,
              }}
              defaultValue={[]}
              onChange={([event]) => {
                setSelectedGroups(event.target.value);
                return event.target.value;
              }}
              input={
                <Input className={classes.multiSelect} id="selectGroupsChip" />
              }
              renderValue={(selected) => (
                <div className={classes.chips}>
                  {selected.map((value) => (
                    <Chip
                      key={value}
                      label={groupIDToName[value]}
                      className={classes.chip}
                    />
                  ))}
                </div>
              )}
              MenuProps={MenuProps}
              multiple
            >
              {groups.length > 0 &&
                groups.map((group) => (
                  <MenuItem
                    value={group.id}
                    key={group.id.toString()}
                    data-testid={`sourcesFilterGroupSelect_${group.id}`}
                    style={getMultiselectStyles(
                      group.id,
                      selectedGroups,
                      theme
                    )}
                  >
                    {group.name}
                  </MenuItem>
                ))}
            </Controller>
          </div>
        )}
        <div className={classes.formButtons}>
          <ButtonGroup
            variant="contained"
            color="primary"
            aria-label="contained primary button group"
          >
            <Button variant="contained" color="primary" type="submit">
              Submit
            </Button>
            <Button
              variant="contained"
              color="primary"
              onClick={handleClickReset}
            >
              Reset
            </Button>
          </ButtonGroup>
        </div>
      </form>
    </Paper>
  );
};

SourceTableFilterForm.propTypes = {
  handleFilterSubmit: PropTypes.func.isRequired,
  groups: PropTypes.arrayOf(PropTypes.shape({})),
};

SourceTableFilterForm.defaultProps = {
  groups: undefined,
};

export default SourceTableFilterForm;
