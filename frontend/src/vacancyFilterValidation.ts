export type VacancyFilterRangeFields = {
  status_date_from: string;
  status_date_to: string;
  total_score_min: string;
  total_score_max: string;
  tasks_min: string;
  tasks_max: string;
  skills_min: string;
  skills_max: string;
  experience_depth_min: string;
  experience_depth_max: string;
  role_match_min: string;
  role_match_max: string;
  industry_min: string;
  industry_max: string;
  special_requirements_min: string;
  special_requirements_max: string;
};

export type VacancyFilterErrors = Partial<Record<keyof VacancyFilterRangeFields, string>>;

const SCORE_RANGE_PAIRS = [
  ["total_score_min", "total_score_max"],
  ["tasks_min", "tasks_max"],
  ["skills_min", "skills_max"],
  ["experience_depth_min", "experience_depth_max"],
  ["role_match_min", "role_match_max"],
  ["industry_min", "industry_max"],
  ["special_requirements_min", "special_requirements_max"],
] as const satisfies ReadonlyArray<readonly [keyof VacancyFilterRangeFields, keyof VacancyFilterRangeFields]>;

export function validateVacancyFilters(filters: VacancyFilterRangeFields): VacancyFilterErrors {
  const errors: VacancyFilterErrors = {};
  const { status_date_from: dateFrom, status_date_to: dateTo } = filters;

  if (dateFrom && dateTo && dateFrom > dateTo) {
    errors.status_date_from = "Дата «От» должна быть не позже даты «До».";
    errors.status_date_to = "Дата «До» должна быть не раньше даты «От».";
  }

  for (const [minimumField, maximumField] of SCORE_RANGE_PAIRS) {
    const minimumRaw = filters[minimumField].trim();
    const maximumRaw = filters[maximumField].trim();
    const minimum = minimumRaw ? Number(minimumRaw) : undefined;
    const maximum = maximumRaw ? Number(maximumRaw) : undefined;
    const validMinimum = minimum === undefined || (Number.isFinite(minimum) && minimum >= 0 && minimum <= 100);
    const validMaximum = maximum === undefined || (Number.isFinite(maximum) && maximum >= 0 && maximum <= 100);

    if (!validMinimum) errors[minimumField] = "Введите число от 0 до 100.";
    if (!validMaximum) errors[maximumField] = "Введите число от 0 до 100.";
    if (validMinimum && validMaximum && minimum !== undefined && maximum !== undefined && minimum > maximum) {
      errors[minimumField] = "Минимум не может быть больше максимума.";
      errors[maximumField] = "Максимум не может быть меньше минимума.";
    }
  }

  return errors;
}
