"""Retrieves load forecast data from Akkudoktor load profiles."""

from pathlib import Path
from typing import Optional

import numpy as np
from pydantic import Field

from akkudoktoreos.config.configabc import SettingsBaseModel
from akkudoktoreos.prediction.loadabc import LoadProvider
from akkudoktoreos.utils.datetimeutil import compare_datetimes, to_datetime, to_duration
from akkudoktoreos.utils.logutil import get_logger

logger = get_logger(__name__)


class LoadAkkudoktorCommonSettings(SettingsBaseModel):
    """Common settings for load data import from file."""

    loadakkudoktor_year_energy: Optional[float] = Field(
        default=None, description="Yearly energy consumption (kWh)."
    )


class LoadAkkudoktor(LoadProvider):
    """Fetch Load forecast data from Akkudoktor load profiles."""

    @classmethod
    def provider_id(cls) -> str:
        """Return the unique identifier for the LoadAkkudoktor provider."""
        return "LoadAkkudoktor"

    def _calculate_adjustment(self, data_year_energy: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Calculate weekday and week end adjustment from total load measurement data.

        Returns:
            weekday_adjust (np.ndarray): hourly adjustment for Monday to Friday.
            weekend_adjust (np.ndarray): hourly adjustment for Saturday and Sunday.
        """
        weekday_adjust = np.zeros(24)
        weekday_adjust_weight = np.zeros(24)
        weekend_adjust = np.zeros(24)
        weekend_adjust_weight = np.zeros(24)

        if self.measurement.max_datetime is None:
            # No measurements - return 0 adjustment
            return (weekday_adjust, weekday_adjust)

        # compare predictions with real measurement - try to use last 7 days
        compare_start = self.measurement.max_datetime - to_duration("7 days")
        if compare_datetimes(compare_start, self.measurement.min_datetime).lt:
            # Not enough measurements for 7 days - use what is available
            compare_start = self.measurement.min_datetime
        compare_end = self.measurement.max_datetime
        compare_interval = to_duration("1 hour")

        load_total_array = self.measurement.load_total(
            start_datetime=compare_start,
            end_datetime=compare_end,
            interval=compare_interval,
        )
        compare_dt = compare_start
        for i in range(len(load_total_array)):
            load_total = load_total_array[i]
            # Extract mean (index 0) and standard deviation (index 1) for the given day and hour
            # Day indexing starts at 0, -1 because of that
            hourly_stats = data_year_energy[compare_dt.day_of_year - 1, :, compare_dt.hour]
            weight = 1 / ((compare_end - compare_dt).days + 1)
            if compare_dt.day_of_week < 5:
                weekday_adjust[compare_dt.hour] += (load_total - hourly_stats[0]) * weight
                weekday_adjust_weight[compare_dt.hour] += weight
            else:
                weekend_adjust[compare_dt.hour] += (load_total - hourly_stats[0]) * weight
                weekend_adjust_weight[compare_dt.hour] += weight
            compare_dt += compare_interval
        # Calculate mean
        for i in range(24):
            if weekday_adjust_weight[i] > 0:
                weekday_adjust[i] = weekday_adjust[i] / weekday_adjust_weight[i]
            if weekend_adjust_weight[i] > 0:
                weekend_adjust[i] = weekend_adjust[i] / weekend_adjust_weight[i]

        return (weekday_adjust, weekend_adjust)

    def load_data(self) -> np.ndarray:
        """Loads data from the Akkudoktor load file."""
        load_file = Path(__file__).parent.parent.joinpath("data/load_profiles.npz")
        data_year_energy = None
        try:
            file_data = np.load(load_file)
            profile_data = np.array(
                list(zip(file_data["yearly_profiles"], file_data["yearly_profiles_std"]))
            )
            # Calculate values in W by relative profile data and yearly consumption given in kWh
            data_year_energy = profile_data * self.config.loadakkudoktor_year_energy * 1000
        except FileNotFoundError:
            error_msg = f"Error: File {load_file} not found."
            logger.error(error_msg)
            raise FileNotFoundError(error_msg)
        except Exception as e:
            error_msg = f"An error occurred while loading data: {e}"
            logger.error(error_msg)
            raise ValueError(error_msg)
        return data_year_energy

    def _update_data(self, force_update: Optional[bool] = False) -> None:
        """Adds the load means and standard deviations."""
        data_year_energy = self.load_data()
        weekday_adjust, weekend_adjust = self._calculate_adjustment(data_year_energy)
        date = self.start_datetime
        for i in range(self.config.prediction_hours):
            # Extract mean (index 0) and standard deviation (index 1) for the given day and hour
            # Day indexing starts at 0, -1 because of that
            hourly_stats = data_year_energy[date.day_of_year - 1, :, date.hour]
            self.update_value(date, "load_mean", hourly_stats[0])
            self.update_value(date, "load_std", hourly_stats[1])
            if date.day_of_week < 5:
                # Monday to Friday (0..4)
                self.update_value(
                    date, "load_mean_adjusted", hourly_stats[0] + weekday_adjust[date.hour]
                )
            else:
                # Saturday, Sunday (5, 6)
                self.update_value(
                    date, "load_mean_adjusted", hourly_stats[0] + weekend_adjust[date.hour]
                )
            date += to_duration("1 hour")
        # We are working on fresh data (no cache), report update time
        self.update_datetime = to_datetime(in_timezone=self.config.timezone)