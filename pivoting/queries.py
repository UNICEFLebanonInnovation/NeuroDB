NEUROREPORT = """
SELECT 
  a.id, a.database_id, a.awp_code, a.ram_result, 
  CASE
    WHEN b.target > 0 THEN b.value * 100 / b.target
    ELSE 0
  END AS achieved,
  b.target awp_target,
  b.* 
from 
  pivoting_masterindicator a, 
  (
    SELECT 
      * 
    from 
      (
        SELECT 
          * 
        from 
          (
            SELECT 
              a.master_id, 
              a.label, 
              a.target, 
              CASE WHEN denominator.value > 0 THEN numerator.value / denominator.value ELSE 0 END AS value 
            FROM 
              pivoting_neuroreportmasterindicator a,
              (
                SELECT 
                  a.master_id AS master_id,
                  b.value AS value
                FROM 
                  pivoting_mastersubindicator a,
                  (
                    SELECT 
                      a.id, 
                      a.NAME, 
                      a.awp_code, 
                      a.target, 
                      CASE WHEN a.aggregation_method = 'SUM' THEN Sum(b.total_ind) WHEN a.aggregation_method = 'AVG' THEN Avg(b.total_ind) END AS value, 
                      Sum(b.reports_count) AS reports_count 
                    FROM 
                      pivoting_subindicator a, 
                      (
                        SELECT 
                          a.id, 
                          a.NAME, 
                          Sum(b.indicator_value) AS total_ind, 
                          Avg(b.indicator_value) AS avg_ind, 
                          Count(*) AS reports_count 
                        FROM 
                          pivoting_indicatornew a, 
                          pivoting_activityreportnew b 
                        WHERE 
                          a.ai_indicator = b.indicator_id 
                          AND a.database_id = b.dbase_id 
                          -- AND b.funded_by = 'UNICEF' 
                          AND b.last_edited_time < '[LAST_EDITED_TIME]'
                          AND b.month_name is NOT NULL
                          AND length(b.month_name) >= 7
                          AND substring(b.month_name from 6 FOR 2)::integer <= [MONTH]
                        GROUP BY 
                          a.id, 
                          a.NAME
                      ) b, 
                      pivoting_subindicator_indicators c 
                    WHERE 
                      a.id = c.subindicator_id 
                      AND c.indicatornew_id = b.id 
                    GROUP BY 
                      a.id
                  ) b 
                WHERE 
                  a.sub_id = b.id 
                  AND a.effect = 'NUMERATOR'
              ) numerator, 
              (
                SELECT 
                  a.master_id AS master_id, 
                  b.value AS value 
                FROM 
                  pivoting_mastersubindicator a, 
                  (
                    SELECT 
                      a.id, 
                      a.NAME, 
                      a.awp_code, 
                      a.target, 
                      CASE WHEN a.aggregation_method = 'SUM' THEN Sum(b.total_ind) WHEN a.aggregation_method = 'AVG' THEN Avg(b.total_ind) END AS value, 
                      Sum(b.reports_count) AS reports_count 
                    FROM 
                      pivoting_subindicator a, 
                      (
                        SELECT 
                          a.id, 
                          a.NAME, 
                          Sum(b.indicator_value) AS total_ind, 
                          Avg(b.indicator_value) AS avg_ind, 
                          Count(*) AS reports_count 
                        FROM 
                          pivoting_indicatornew a, 
                          pivoting_activityreportnew b 
                        WHERE 
                          a.ai_indicator = b.indicator_id 
                          AND a.database_id = b.dbase_id 
                          --AND b.funded_by = 'UNICEF' 
                          AND b.last_edited_time < '[LAST_EDITED_TIME]'
                          AND b.month_name is NOT NULL
                          AND length(b.month_name) >= 7
                          AND substring(b.month_name from 6 FOR 2)::integer <= [MONTH]
                        GROUP BY 
                          a.id, 
                          a.NAME
                      ) b, 
                      pivoting_subindicator_indicators c 
                    WHERE 
                      a.id = c.subindicator_id 
                      AND c.indicatornew_id = b.id 
                    GROUP BY 
                      a.id
                  ) b 
                WHERE 
                  a.sub_id = b.id 
                  AND a.effect = 'DENOMINATOR'
              ) denominator 
            WHERE 
              a.master_id = numerator.master_id 
              AND a.master_id = denominator.master_id 
            UNION 
            SELECT 
              a.master_id, 
              a.label, 
              a.target, 
              b.value 
            FROM 
              pivoting_neuroreportmasterindicator a 
              LEFT JOIN (
                SELECT 
                  a.id, 
                  CASE WHEN a.aggregation_method = 'SUM' THEN Sum(b.value) WHEN a.aggregation_method = 'AVERAGE' THEN Avg(b.value) WHEN a.aggregation_method = 'MINIMUM' THEN Min(b.value) WHEN a.aggregation_method = 'MAXIMUM' THEN Max(b.value) END AS value, 
                  Sum(b.value) * 100 / a.awp_target AS achieved, 
                  Sum(b.target) AS total_targets, 
                  Sum(b.reports_count) AS reports_count 
                FROM 
                  pivoting_masterindicator a, 
                  (
                    SELECT 
                      a.id, 
                      a.NAME, 
                      a.awp_code, 
                      a.target, 
                      CASE WHEN a.aggregation_method = 'SUM' THEN Sum(b.total_ind) WHEN a.aggregation_method = 'AVG' THEN Avg(b.total_ind) END AS value, 
                      Sum(b.reports_count) AS reports_count 
                    FROM 
                      pivoting_subindicator a, 
                      (
                        SELECT 
                          a.id, 
                          a.NAME, 
                          Sum(b.indicator_value) AS total_ind, 
                          Avg(b.indicator_value) AS avg_ind, 
                          Count(*) AS reports_count 
                        FROM 
                          pivoting_indicatornew a, 
                          pivoting_activityreportnew b 
                        WHERE 
                          a.ai_indicator = b.indicator_id 
                          AND a.database_id = b.dbase_id 
                          --AND b.funded_by = 'UNICEF' 
                          AND b.last_edited_time < '[LAST_EDITED_TIME]'
                          AND b.month_name is NOT NULL
                          AND length(b.month_name) >= 7
                          AND substring(b.month_name from 6 FOR 2)::integer <= [MONTH]
                        GROUP BY 
                          a.id, 
                          a.NAME
                      ) b, 
                      pivoting_subindicator_indicators c 
                    WHERE 
                      a.id = c.subindicator_id 
                      AND c.indicatornew_id = b.id 
                    GROUP BY 
                      a.id
                  ) b, 
                  pivoting_mastersubindicator c 
                WHERE 
                  a.id = c.master_id 
                  AND c.sub_id = b.id 
                  AND a.aggregation_method IN (
                    'SUM', 'AVERAGE', 'MINIMUM', 'MAXIMUM'
                  ) 
                  AND c.effect = 'TOTAL' 
                GROUP BY 
                  a.id
              ) b ON a.master_id = b.id 
            WHERE 
              report_id = [REPORT_ID]
          ) master 
          LEFT JOIN (
            SELECT 
              a.master_id mid, 
              sum(g.indicator_value) males 
            from 
              pivoting_neuroreportmasterindicator a, 
              pivoting_masterindicator b, 
              pivoting_mastersubindicator c, 
              pivoting_subindicator d, 
              pivoting_subindicator_indicators e, 
              pivoting_indicatornew f, 
              pivoting_activityreportnew g 
            where 
              a.report_id = [REPORT_ID] 
              and a.master_id = b.id 
              and b.id = c.master_id 
              AND b.aggregation_method = 'SUM'
              and c.effect = 'TOTAL' 
              and c.sub_id = d.id 
              and d.id = e.subindicator_id 
              and e.indicatornew_id = f.id 
              and f.ai_indicator = g.indicator_id 
              and f.gender = 'Male' 
              --AND g.funded_by = 'UNICEF' 
              AND g.last_edited_time < '[LAST_EDITED_TIME]'
              AND g.month_name is NOT NULL
              AND length(g.month_name) >= 7
              AND substring(g.month_name from 6 FOR 2)::integer <= [MONTH]
              AND f.database_id = g.dbase_id
            group by 
              a.master_id
          ) genders on master.master_id = genders.mid
      ) master 
      LEFT JOIN (
        SELECT 
          a.master_id mid, 
          sum(g.indicator_value) females 
        from 
          pivoting_neuroreportmasterindicator a, 
          pivoting_masterindicator b, 
          pivoting_mastersubindicator c, 
          pivoting_subindicator d, 
          pivoting_subindicator_indicators e, 
          pivoting_indicatornew f, 
          pivoting_activityreportnew g 
        where 
          a.report_id = [REPORT_ID] 
          and a.master_id = b.id 
          and b.id = c.master_id 
          AND b.aggregation_method = 'SUM'
          and c.effect = 'TOTAL' 
          and c.sub_id = d.id 
          and d.id = e.subindicator_id 
          and e.indicatornew_id = f.id 
          and f.ai_indicator = g.indicator_id 
          and f.gender = 'Female' 
          --AND g.funded_by = 'UNICEF' 
          AND g.last_edited_time < '[LAST_EDITED_TIME]'
          AND g.month_name is NOT NULL
          AND length(g.month_name) >= 7
          AND substring(g.month_name from 6 FOR 2)::integer <= [MONTH]
          AND f.database_id = g.dbase_id
        group by 
          a.master_id
      ) genders on master.master_id = genders.mid
  ) b 
where 
  a.id = b.master_id

"""

MASTER_SUB_INDICATORS = """
SELECT a.id AS master_id,
       b.*,
       c.label
FROM   pivoting_masterindicator a,
       (SELECT a.id,
               a.NAME,
               a.awp_code,
               a.target,
               Sum(b.total_ind)     AS total_sub,
               Avg(b.total_ind)     AS avg_sub,
               Avg(b.avg_ind)       AS avg_of_avg,
               Sum(b.reports_count) AS reports_count
        FROM   pivoting_subindicator a,
               (SELECT a.id,
                       a.NAME,
                       Sum(b.indicator_value) AS total_ind,
                       Avg(b.indicator_value) AS avg_ind,
                       Count(*)               AS reports_count
                FROM   pivoting_indicatornew a,
                       pivoting_activityreportnew b
                WHERE  a.ai_indicator = b.indicator_id
                       AND a.database_id = b.dbase_id
                       --AND b.funded_by = 'UNICEF'
                GROUP  BY a.id,
                          a.NAME) b,
               pivoting_subindicator_indicators c
        WHERE  a.id = c.subindicator_id
               AND c.indicatornew_id = b.id
        GROUP  BY a.id) b,
       pivoting_mastersubindicator c
WHERE  a.id = c.master_id
       AND c.sub_id = b.id
       AND a.id = %s 
ORDER BY c.sequence asc
"""

DATABASE_ACTIVITYINFO_OLD = """
SELECT Concat(b.awp_code,'_',b.NAME) AS "Master Indicator",
       CASE
              WHEN b.awp_target == 0 THEN 0
              ELSE b.awp_target
       END                                  AS target,
       --b.indicator_type as category,
       --b.unit,
       --b.sector_equivalent,
       bc.label AS "Sub Indicator",
       CASE
              WHEN bc.effect = 'TOTAL' THEN 'Include values that effect master indicatotors'
              ELSE 'Include values that do not effect master indicatotors'
       END                                  AS "Value Cal.",
       --bc.effect,
       --d.awp_code,
       --d.type,
       --d.units,
       --d.activity_id,
       d.disability,
       d.gender,
       d.nationality,
       d.age_group as age,
       d.programme,
       d.name  "AI Indicators",
       --e.form,
       e.indicator_value,
       --e.indicator_awp_code,
       e.location_adminlevel_cadastral_area,
       e.location_adminlevel_caza        district,
       e.location_adminlevel_governorate "gov.",
       e.partner_label             partner,
       e.project_label  PD,
       e.project_plan  Plan,
       --e.parent_form activity,
       --e.reporting_section,
       CASE
              WHEN substring(e.month_name from 6 FOR 2) IN ('01') THEN 'Jan'
              WHEN substring(e.month_name FROM 6 FOR 2) IN ('02') THEN 'Feb'
              WHEN substring(e.month_name FROM 6 FOR 2) IN ('03') THEN 'Mar'
              WHEN substring(e.month_name FROM 6 FOR 2) IN ('04') THEN 'Apr'
              WHEN substring(e.month_name FROM 6 FOR 2) IN ('05') THEN 'May'
              WHEN substring(e.month_name FROM 6 FOR 2) IN ('06') THEN 'Jun'
              WHEN substring(e.month_name FROM 6 FOR 2) IN ('07') THEN 'Jul'
              WHEN substring(e.month_name FROM 6 FOR 2) IN ('08') THEN 'Aug'
              WHEN substring(e.month_name FROM 6 FOR 2) IN ('09') THEN 'Sep'
              WHEN substring(e.month_name FROM 6 FOR 2) IN ('10') THEN 'Oct'
              WHEN substring(e.month_name FROM 6 FOR 2) IN ('11') THEN 'Nov'
              WHEN substring(e.month_name FROM 6 FOR 2) IN ('12') THEN 'Dec'
       END AS month
FROM   pivoting_masterindicator b,
       pivoting_mastersubindicator bc,
       pivoting_subindicator c,
       pivoting_subindicator_indicators cd,
       pivoting_indicatornew d,
       pivoting_activityreportnew e
WHERE  b.database_id = %s
--AND    e.funded_by = 'UNICEF'
AND    b.id = bc.master_id
AND    bc.sub_id = c.id
AND    c.id = cd.subindicator_id
AND    cd.indicatornew_id = d.id
AND    d.ai_indicator = e.indicator_id
AND    e.dbase_id = b.database_id"""

DATABASE_ACTIVITYINFO = """
select
  MAX(age) age,
  MAX(AIIndicators) as "AI Indicators",
  MAX(indicator_awp_code) as "AWP Code",
  MAX(IndicatorName) as "Indicator",
  MAX(disability) disability,
  MAX(district) district,
  MAX(gender) gender,
  MAX(gov) as "gov.",
  SUM(indicator_value) indicator_value,
  MAX(location_adminlevel_cadastral_area) location_adminlevel_cadastral_area,
  MAX(MasterIndicator) as "Master Indicator",
  MAX(month) as "month",
  MAX(nationality) nationality,
  MAX(partner) partner,
  MAX(pd) pd,
  MAX(plan) plan,
  MAX(project) project,
  MAX(programme) programme,
  MAX(SubIndicator) as "Sub Indicator",
  MAX(target) target,
  MAX(emergency) emergency,
  MAX(ValueCal) as "Value Cal.",
  MAX(Sequence) as "Sequence",
  MAX(databaseid) as "databaseid"
from
  (
    SELECT
      Concat(b.awp_code, '_', b.NAME) AS MasterIndicator,
      CASE
        WHEN b.awp_target = 0 THEN 0
        ELSE b.awp_target
      END AS target,
      bc.label AS SubIndicator,
      bc.sequence AS Sequence,
      CASE
        WHEN bc.effect = 'TOTAL' THEN 'Include values that affect master indicators'
        ELSE 'Include values that do not affect master indicators'
      END AS ValueCal,
      d.disability,
      d.gender,
      d.nationality,
      d.age_group as age,
      d.programme,
      d.name AIIndicators,
      e.indicator_name IndicatorName,
      e.indicator_awp_code,
      e.emergency,
      e.indicator_value,
      e.location_adminlevel_cadastral_area,
      e.location_adminlevel_caza district,
      e.location_adminlevel_governorate Gov,
      e.partner_label partner,
      e.project_label PD,
      e.project_plan Plan,
      e.project Project,
      e.database_ai_id databaseid,
      --e.parent_form activity,
      --e.reporting_section,
      CASE
        WHEN substring(
          e.month_name
          from
            6 FOR 2
        ) IN ('01') THEN 'Jan'
        WHEN substring(
          e.month_name
          FROM
            6 FOR 2
        ) IN ('02') THEN 'Feb'
        WHEN substring(
          e.month_name
          FROM
            6 FOR 2
        ) IN ('03') THEN 'Mar'
        WHEN substring(
          e.month_name
          FROM
            6 FOR 2
        ) IN ('04') THEN 'Apr'
        WHEN substring(
          e.month_name
          FROM
            6 FOR 2
        ) IN ('05') THEN 'May'
        WHEN substring(
          e.month_name
          FROM
            6 FOR 2
        ) IN ('06') THEN 'Jun'
        WHEN substring(
          e.month_name
          FROM
            6 FOR 2
        ) IN ('07') THEN 'Jul'
        WHEN substring(
          e.month_name
          FROM
            6 FOR 2
        ) IN ('08') THEN 'Aug'
        WHEN substring(
          e.month_name
          FROM
            6 FOR 2
        ) IN ('09') THEN 'Sep'
        WHEN substring(
          e.month_name
          FROM
            6 FOR 2
        ) IN ('10') THEN 'Oct'
        WHEN substring(
          e.month_name
          FROM
            6 FOR 2
        ) IN ('11') THEN 'Nov'
        WHEN substring(
          e.month_name
          FROM
            6 FOR 2
        ) IN ('12') THEN 'Dec'
      END AS month
    FROM
      pivoting_masterindicator b,
      pivoting_mastersubindicator bc,
      pivoting_subindicator c,
      pivoting_subindicator_indicators cd,
      pivoting_indicatornew d,
      pivoting_activityreportnew e
    WHERE
      b.database_id = %s
      AND b.is_active = true
      --AND e.funded_by = 'UNICEF'
      AND b.id = bc.master_id
      AND bc.sub_id = c.id
      AND c.id = cd.subindicator_id
      AND cd.indicatornew_id = d.id
      AND d.ai_indicator = e.indicator_id
      AND e.dbase_id = b.database_id
      AND e.indicator_value > 0
  ) a
group by
  a.age,
  a.AIIndicators,
  a.indicator_awp_code,
  a.IndicatorName,
  a.disability,
  a.district,
  a.gender,
  a.gov,
  a.location_adminlevel_cadastral_area,
  a.MasterIndicator,
  a.month,
  a.nationality,
  a.partner,
  a.pd,
  a.plan,
  a.project,
  a.programme,
  a.SubIndicator,
  a.target,
  a.ValueCal,
  a.emergency,
  a.Sequence,
  a.databaseid
"""


NEUROREPORT_ACTIVITYINFO = """
select
  MAX(age) age,
  MAX(AIIndicators) as "AI Indicators",
  MAX(disability) disability,
  MAX(district) district,
  MAX(gender) gender,
  MAX(gov) as "gov.",
  SUM(indicator_value) indicator_value,
  MAX(location_adminlevel_cadastral_area) location_adminlevel_cadastral_area,
  MAX(MasterIndicator) as "Master Indicator",
  MAX(month) as "month",
  MAX(nationality) nationality,
  MAX(partner) partner,
  MAX(pd) pd,
  MAX(plan) plan,
  MAX(project) project,
  MAX(programme) programme,
  MAX(SubIndicator) as "Sub Indicator",
  MAX(target) target,
  MAX(emergency) emergency,
  MAX(section) section,
  MAX(ValueCal) as "Value Cal.",
  MAX(Sequence) as "Sequence"
from
  (
    SELECT
      Concat(b.awp_code, '_', b.NAME) AS MasterIndicator,
      CASE
        WHEN b.awp_target = 0 THEN 0
        ELSE b.awp_target
      END AS target,
      bc.label AS SubIndicator,
      bc.sequence AS Sequence,
      CASE
        WHEN bc.effect = 'TOTAL' THEN 'Include values that affect master indicators'
        ELSE 'Include values that do not affect master indicators'
      END AS ValueCal,
      d.disability,
      d.gender,
      d.nationality,
      d.age_group as age,
      d.programme,
      d.name AIIndicators,
      e.emergency,
      e.indicator_value,
      e.location_adminlevel_cadastral_area,
      e.location_adminlevel_caza district,
      e.location_adminlevel_governorate Gov,
      e.partner_label partner,
      e.project_label PD,
      e.project_plan Plan,
      e.project Project,
      h.label as section,
      --e.parent_form activity,
      --e.reporting_section,
      CASE
        WHEN substring(
          e.month_name
          from
            6 FOR 2
        ) IN ('01') THEN 'Jan'
        WHEN substring(
          e.month_name
          FROM
            6 FOR 2
        ) IN ('02') THEN 'Feb'
        WHEN substring(
          e.month_name
          FROM
            6 FOR 2
        ) IN ('03') THEN 'Mar'
        WHEN substring(
          e.month_name
          FROM
            6 FOR 2
        ) IN ('04') THEN 'Apr'
        WHEN substring(
          e.month_name
          FROM
            6 FOR 2
        ) IN ('05') THEN 'May'
        WHEN substring(
          e.month_name
          FROM
            6 FOR 2
        ) IN ('06') THEN 'Jun'
        WHEN substring(
          e.month_name
          FROM
            6 FOR 2
        ) IN ('07') THEN 'Jul'
        WHEN substring(
          e.month_name
          FROM
            6 FOR 2
        ) IN ('08') THEN 'Aug'
        WHEN substring(
          e.month_name
          FROM
            6 FOR 2
        ) IN ('09') THEN 'Sep'
        WHEN substring(
          e.month_name
          FROM
            6 FOR 2
        ) IN ('10') THEN 'Oct'
        WHEN substring(
          e.month_name
          FROM
            6 FOR 2
        ) IN ('11') THEN 'Nov'
        WHEN substring(
          e.month_name
          FROM
            6 FOR 2
        ) IN ('12') THEN 'Dec'
      END AS month
FROM   pivoting_masterindicator b,
       pivoting_mastersubindicator bc,
       pivoting_subindicator c,
       pivoting_subindicator_indicators cd,
       pivoting_indicatornew d,
       pivoting_activityreportnew e,
       pivoting_neuroreportmasterindicator f,
       pivoting_neuroreport g,
       pivoting_database h
WHERE  g.id= %s
AND    e.emergency in (EMERGENCY_VALUES)
--AND    e.funded_by = 'UNICEF'
AND    f.report_id = g.id
AND    f.master_id = b.id
AND    b.id = bc.master_id
AND    bc.sub_id = c.id
AND    c.id = cd.subindicator_id
AND    cd.indicatornew_id = d.id
AND    d.ai_indicator = e.indicator_id
AND    e.dbase_id = b.database_id
AND    h.id = b.database_id  ) a
group by
  a.age,
  a.AIIndicators,
  a.disability,
  a.district,
  a.gender,
  a.gov,
  a.section,
  a.location_adminlevel_cadastral_area,
  a.MasterIndicator,
  a.month,
  a.nationality,
  a.partner,
  a.project,
  a.emergency,
  a.pd,
  a.plan,
  a.programme,
  a.SubIndicator,
  a.target,
  a.ValueCal,
  a.Sequence
"""

PCA_ACTIVITYINFO = """
SELECT a.label          Database_Name,
       b.awp_code,
       b.type,
       b.units,
       b.activity_id,
       b.disability,
       b.gender,
       b.nationality,
       c.form,
       c.indicator_value,
       c.indicator_awp_code,
       c.location_adminlevel_cadastral_area,
       c.location_adminlevel_caza        caza,
       c.location_adminlevel_governorate governorate,
       c.partner_label             partner,
       c.project_label  PDs,
       c.parent_form activity,
       c.reporting_section,
       c.id                         report_id,
       substring(c.month_name FROM 1 FOR 4)  Reporting_Year,
       CASE
              WHEN substring(c.month_name from 6 FOR 2) IN ('01') THEN 'Jan'
              WHEN substring(c.month_name FROM 6 FOR 2) IN ('02') THEN 'Feb'
              WHEN substring(c.month_name FROM 6 FOR 2) IN ('03') THEN 'Mar'
              WHEN substring(c.month_name FROM 6 FOR 2) IN ('04') THEN 'Apr'
              WHEN substring(c.month_name FROM 6 FOR 2) IN ('05') THEN 'May'
              WHEN substring(c.month_name FROM 6 FOR 2) IN ('06') THEN 'Jun'
              WHEN substring(c.month_name FROM 6 FOR 2) IN ('07') THEN 'Jul'
              WHEN substring(c.month_name FROM 6 FOR 2) IN ('08') THEN 'Aug'
              WHEN substring(c.month_name FROM 6 FOR 2) IN ('09') THEN 'Sep'
              WHEN substring(c.month_name FROM 6 FOR 2) IN ('10') THEN 'Oct'
              WHEN substring(c.month_name FROM 6 FOR 2) IN ('11') THEN 'Nov'
              WHEN substring(c.month_name FROM 6 FOR 2) IN ('12') THEN 'Dec'
       END AS month
FROM    pivoting_database a,
        pivoting_indicatornew b,
        pivoting_activityreportnew c,
        etools_pca d
WHERE   a.id = b.database_id
AND     a.id = c.dbase_id
AND     b.ai_indicator = c.indicator_id
--AND     c.funded_by = 'UNICEF'
AND     LENGTH(c.project_label) > 10
AND     d.id = %s
AND     TRIM(d.number) LIKE TRIM(c.project_label) || '%%' """

PCA_ACTIVITYINFO_PURE = """
SELECT a.label          section,
       c.form,
       c.indicator_name as "activityinfo_indicator",
       c.indicator_value,
       c.indicator_awp_code,
       c.location_adminlevel_cadastral_area   cadaster,
       c.location_adminlevel_caza        district,
       c.location_adminlevel_governorate governorate,
       c.partner_label             partner,
       c.project_label  PDs,
       c.parent_form activity,
       c.reporting_section,
       c.id                         report_id,
       substring(c.month_name FROM 1 FOR 4)  Reporting_Year,
       CASE
              WHEN substring(c.month_name from 6 FOR 2) IN ('01') THEN 'Jan'
              WHEN substring(c.month_name FROM 6 FOR 2) IN ('02') THEN 'Feb'
              WHEN substring(c.month_name FROM 6 FOR 2) IN ('03') THEN 'Mar'
              WHEN substring(c.month_name FROM 6 FOR 2) IN ('04') THEN 'Apr'
              WHEN substring(c.month_name FROM 6 FOR 2) IN ('05') THEN 'May'
              WHEN substring(c.month_name FROM 6 FOR 2) IN ('06') THEN 'Jun'
              WHEN substring(c.month_name FROM 6 FOR 2) IN ('07') THEN 'Jul'
              WHEN substring(c.month_name FROM 6 FOR 2) IN ('08') THEN 'Aug'
              WHEN substring(c.month_name FROM 6 FOR 2) IN ('09') THEN 'Sep'
              WHEN substring(c.month_name FROM 6 FOR 2) IN ('10') THEN 'Oct'
              WHEN substring(c.month_name FROM 6 FOR 2) IN ('11') THEN 'Nov'
              WHEN substring(c.month_name FROM 6 FOR 2) IN ('12') THEN 'Dec'
       END AS month
FROM    pivoting_database a,
        pivoting_activityreportnew c,
        etools_pca d
WHERE   a.id = c.dbase_id
--AND     c.funded_by = 'UNICEF'
AND     LENGTH(c.project_label) > 10
AND     d.id = %s
AND     TRIM(d.number) LIKE TRIM(c.project_label) || '%%' """

DONOR_ACTIVITYINFO_PURE = """
SELECT a.label section, 
      c.indicator_name,
       c.form,
       c.indicator_value,
       c.indicator_awp_code,
       c.location_adminlevel_cadastral_area   cadastral,
       c.location_adminlevel_caza        caza,
       c.location_adminlevel_governorate governorate,
       c.partner_label             partner,
       c.project_label  PDs,
       c.parent_form activity,
       c.reporting_section,
       c.id                         report_id,
       substring(c.month_name FROM 1 FOR 4)  Reporting_Year,
       CASE
              WHEN substring(c.month_name from 6 FOR 2) IN ('01') THEN 'Jan'
              WHEN substring(c.month_name FROM 6 FOR 2) IN ('02') THEN 'Feb'
              WHEN substring(c.month_name FROM 6 FOR 2) IN ('03') THEN 'Mar'
              WHEN substring(c.month_name FROM 6 FOR 2) IN ('04') THEN 'Apr'
              WHEN substring(c.month_name FROM 6 FOR 2) IN ('05') THEN 'May'
              WHEN substring(c.month_name FROM 6 FOR 2) IN ('06') THEN 'Jun'
              WHEN substring(c.month_name FROM 6 FOR 2) IN ('07') THEN 'Jul'
              WHEN substring(c.month_name FROM 6 FOR 2) IN ('08') THEN 'Aug'
              WHEN substring(c.month_name FROM 6 FOR 2) IN ('09') THEN 'Sep'
              WHEN substring(c.month_name FROM 6 FOR 2) IN ('10') THEN 'Oct'
              WHEN substring(c.month_name FROM 6 FOR 2) IN ('11') THEN 'Nov'
              WHEN substring(c.month_name FROM 6 FOR 2) IN ('12') THEN 'Dec'
       END AS month
FROM    pivoting_database a,
       
        pivoting_activityreportnew c,
        etools_pca d
WHERE   a.id = c.dbase_id
--AND     c.funded_by = 'UNICEF'
AND     LENGTH(c.project_label) > 10
AND     %s = ANY(donors)
AND     TRIM(d.number) LIKE TRIM(c.project_label) || '%%' """

MASTER_INDICATORS = """
SELECT a.sequence,
       a.id,
       a.NAME,
       a.awp_code,
       a.awp_target,
       a.ram_result,
       CASE
         WHEN denominator.value > 0 THEN numerator.value / denominator.value
         ELSE 0
       END  AS value,
       CASE
         WHEN denominator.value > 0 THEN numerator.value / denominator.value
         ELSE 0
       END  AS achieved,
       100  AS total_targets,
       0    AS reports_count
FROM   pivoting_masterindicator a,
       (SELECT a.master_id AS master_id,
               b.value     AS value
        FROM   pivoting_mastersubindicator a,
               (SELECT a.id,
                       a.NAME,
                       a.awp_code,
                       a.target,
                       CASE
                         WHEN a.aggregation_method = 'SUM' THEN Sum(b.total_ind)
                         WHEN a.aggregation_method = 'AVG' THEN Avg(b.total_ind)
                       END                  AS value,
                       Sum(b.reports_count) AS reports_count
                FROM   pivoting_subindicator a,
                       (SELECT a.id,
                               a.NAME,
                               Sum(b.indicator_value) AS total_ind,
                               Avg(b.indicator_value) AS avg_ind,
                               Count(*)               AS reports_count
                        FROM   pivoting_indicatornew a,
                               pivoting_activityreportnew b
                        WHERE  a.ai_indicator = b.indicator_id
                               AND a.database_id = b.dbase_id
                               --AND b.funded_by = 'UNICEF'
                        GROUP  BY a.id,
                                  a.NAME) b,
                       pivoting_subindicator_indicators c
                WHERE  a.id = c.subindicator_id
                       AND c.indicatornew_id = b.id
                GROUP  BY a.id) b
        WHERE  a.sub_id = b.id
               AND a.effect = 'NUMERATOR') numerator,
       (SELECT a.master_id AS master_id,
               b.value     AS value
        FROM   pivoting_mastersubindicator a,
               (SELECT a.id,
                       a.NAME,
                       a.awp_code,
                       a.target,
                       CASE
                         WHEN a.aggregation_method = 'SUM' THEN Sum(b.total_ind)
                         WHEN a.aggregation_method = 'AVG' THEN Avg(b.total_ind)
                       END                  AS value,
                       Sum(b.reports_count) AS reports_count
                FROM   pivoting_subindicator a,
                       (SELECT a.id,
                               a.NAME,
                               Sum(b.indicator_value) AS total_ind,
                               Avg(b.indicator_value) AS avg_ind,
                               Count(*)               AS reports_count
                        FROM   pivoting_indicatornew a,
                               pivoting_activityreportnew b
                        WHERE  a.ai_indicator = b.indicator_id
                               AND a.database_id = b.dbase_id
                               --AND b.funded_by = 'UNICEF'
                        GROUP  BY a.id,
                                  a.NAME) b,
                       pivoting_subindicator_indicators c
                WHERE  a.id = c.subindicator_id
                       AND c.indicatornew_id = b.id
                GROUP  BY a.id) b
        WHERE  a.sub_id = b.id
               AND a.effect = 'DENOMINATOR') denominator
WHERE  a.id = numerator.master_id
       AND a.id = denominator.master_id
       AND a.database_id = %s
UNION
SELECT a.sequence,
       a.id,
       a.NAME,
       a.awp_code,
       a.awp_target,
       a.ram_result,
       b.value,
       b.achieved,
       b.total_targets,
       b.reports_count
FROM   pivoting_masterindicator a
       LEFT JOIN (SELECT a.id,
                         CASE
                           WHEN a.aggregation_method = 'SUM' THEN Sum(b.value)
                           WHEN a.aggregation_method = 'AVERAGE' THEN
                           Avg(b.value)
                           WHEN a.aggregation_method = 'MINIMUM' THEN
                           Min(b.value)
                           WHEN a.aggregation_method = 'MAXIMUM' THEN
                           Max(b.value)
                         END                               AS value,
                         -- awp_target could be zero
                         CASE
                           WHEN a.awp_target > 0 AND a.aggregation_method = 'SUM' THEN Sum(b.value) * 100 / a.awp_target
						   WHEN a.awp_target > 0 AND a.aggregation_method = 'AVERAGE' THEN Avg(b.value) * 100 / a.awp_target
						   WHEN a.awp_target > 0 AND a.aggregation_method = 'MINIMUM' THEN Min(b.value) * 100 / a.awp_target
						   WHEN a.awp_target > 0 AND a.aggregation_method = 'MAXIMUM' THEN Max(b.value) * 100 / a.awp_target
                           ELSE 0
                         END AS achieved,
                         Sum(b.target)                     AS total_targets,
                         Sum(b.reports_count)              AS reports_count
                  FROM   pivoting_masterindicator a,
                         (SELECT a.id,
                                 a.NAME,
                                 a.awp_code,
                                 a.target,
                                 CASE
                                   WHEN a.aggregation_method = 'SUM' THEN
                                   Sum(b.total_ind)
                                   WHEN a.aggregation_method = 'AVG' THEN
                                   Avg(b.total_ind)
                                 END                  AS value,
                                 Sum(b.reports_count) AS reports_count
                          FROM   pivoting_subindicator a,
                                 (SELECT a.id,
                                         a.NAME,
                                         Sum(b.indicator_value) AS total_ind,
                                         Avg(b.indicator_value) AS avg_ind,
                                         Count(*)               AS reports_count
                                  FROM   pivoting_indicatornew a,
                                         pivoting_activityreportnew b
                                  WHERE  a.ai_indicator = b.indicator_id
                                         AND a.database_id = b.dbase_id
                                         --AND b.funded_by = 'UNICEF'
                                  GROUP  BY a.id,
                                            a.NAME) b,
                                 pivoting_subindicator_indicators c
                          WHERE  a.id = c.subindicator_id
                                 AND c.indicatornew_id = b.id
                          GROUP  BY a.id) b,
                         pivoting_mastersubindicator c
                  WHERE  a.id = c.master_id
                         AND a.database_id = %s
                         AND c.sub_id = b.id
                         AND a.aggregation_method IN ( 'SUM', 'AVERAGE', 'MINIMUM', 'MAXIMUM' )
                         AND c.effect = 'TOTAL'
                  GROUP  BY a.id) b
              ON a.id = b.id
WHERE  a.database_id = %s 
"""

MASTER_INDICATORS_SUM = """
SELECT a.*,
       b.value,
       b.avg_ind,
       b.reports_count
FROM   pivoting_masterindicator a
       LEFT JOIN (SELECT f.id,
                         Sum(b.indicator_value) AS value,
                         Avg(b.indicator_value) AS avg_ind,
                         Count(*)               AS reports_count
                  FROM   pivoting_indicatornew a,
                         pivoting_activityreportnew b,
                         pivoting_subindicator_indicators c,
                         pivoting_subindicator d,
                         pivoting_mastersubindicator e,
                         pivoting_masterindicator f
                  WHERE  a.ai_indicator = b.indicator_id
                         AND a.database_id = b.dbase_id
                         AND c.indicatornew_id = a.id
                         AND c.subindicator_id = d.id
                         AND d.id = e.sub_id
                         AND e.master_id = f.id
                         --AND b.funded_by = 'UNICEF'
                         AND f.database_id = %s
                         AND f.aggregation_method = 'SUM'
                         AND e.effect = 'TOTAL'
                  GROUP  BY f.id) b
              ON a.id = b.id
WHERE  a.database_id = %s AND a.aggregation_method = 'SUM' AND a.is_active = true
"""

MASTER_INDICATORS_MAXIMUM = """
SELECT a.*,
       b.value
FROM   pivoting_masterindicator a
       LEFT JOIN (SELECT b.id,
                         Max(b.value) AS value
                  FROM   (SELECT f.id,
                                 b.month_name,
                                 Sum(b.indicator_value) AS value,
                                 Avg(b.indicator_value) AS avg_ind,
                                 Count(*)               AS reports_count
                          FROM   pivoting_indicatornew a,
                                 pivoting_activityreportnew b,
                                 pivoting_subindicator_indicators c,
                                 pivoting_subindicator d,
                                 pivoting_mastersubindicator e,
                                 pivoting_masterindicator f
                          WHERE  a.ai_indicator = b.indicator_id
                                 AND a.database_id = b.dbase_id
                                 AND c.indicatornew_id = a.id
                                 AND c.subindicator_id = d.id
                                 AND d.id = e.sub_id
                                 AND e.master_id = f.id
                                 --AND b.funded_by = 'UNICEF'
                                 AND f.database_id = %s
                                 AND f.aggregation_method = 'MAXIMUM'
                                 AND e.effect = 'TOTAL'
                          GROUP  BY f.id,
                                    b.month_name) b
                  GROUP  BY b.id) b
              ON a.id = b.id
WHERE  a.database_id = %s
       AND a.aggregation_method = 'MAXIMUM' 
       AND a.is_active = true
"""

MASTER_INDICATORS_AVERAGE = """
SELECT a.*,
       b.value
FROM   pivoting_masterindicator a
       LEFT JOIN (SELECT b.id,
                         Avg(b.value) AS value
                  FROM   (SELECT f.id,
                                 b.month_name,
                                 Sum(b.indicator_value) AS value,
                                 Avg(b.indicator_value) AS avg_ind,
                                 Count(*)               AS reports_count
                          FROM   pivoting_indicatornew a,
                                 pivoting_activityreportnew b,
                                 pivoting_subindicator_indicators c,
                                 pivoting_subindicator d,
                                 pivoting_mastersubindicator e,
                                 pivoting_masterindicator f
                          WHERE  a.ai_indicator = b.indicator_id
                                 AND a.database_id = b.dbase_id
                                 AND c.indicatornew_id = a.id
                                 AND c.subindicator_id = d.id
                                 AND d.id = e.sub_id
                                 AND e.master_id = f.id
                                 --AND b.funded_by = 'UNICEF'
                                 AND f.database_id = %s
                                 AND f.aggregation_method = 'AVERAGE'
                                 AND e.effect = 'TOTAL'
                          GROUP  BY f.id,
                                    b.month_name) b
                  GROUP  BY b.id) b
              ON a.id = b.id
WHERE  a.database_id = %s
       AND a.aggregation_method = 'AVERAGE' 
       AND a.is_active = true
"""

MASTER_INDICATORS_COUNT = """
SELECT a.*,
       b.value
FROM   pivoting_masterindicator a
       LEFT JOIN (SELECT b.id,
                         Avg(b.value) AS value
                  FROM   (SELECT f.id,
                                 b.month_name,
                                 --Sum(b.indicator_value) AS value,
                                 Count(*) AS value,
                                 Avg(b.indicator_value) AS avg_ind,
                                 Count(*)               AS reports_count
                          FROM   pivoting_indicatornew a,
                                 pivoting_activityreportnew b,
                                 pivoting_subindicator_indicators c,
                                 pivoting_subindicator d,
                                 pivoting_mastersubindicator e,
                                 pivoting_masterindicator f
                          WHERE  a.ai_indicator = b.indicator_id
                                 AND a.database_id = b.dbase_id
                                 AND c.indicatornew_id = a.id
                                 AND c.subindicator_id = d.id
                                 AND d.id = e.sub_id
                                 AND e.master_id = f.id
                                 --AND b.funded_by = 'UNICEF'
                                 AND f.database_id = %s
                                 AND f.aggregation_method = 'COUNT'
                                 AND e.effect = 'TOTAL'
                          GROUP  BY f.id,
                                    b.month_name) b
                  GROUP  BY b.id) b
              ON a.id = b.id
WHERE  a.database_id = %s
       AND a.aggregation_method = 'COUNT' 
       AND a.is_active = true

"""

PCA_UNNESTED_DONORS = """
SELECT 
pca.id,
pca.partner_name,
-- pca.document_type,
-- pca.country_programme,
pca.number,
-- pca.title,
-- pca.project_type,
-- pca.fr_number,
-- pca.actual_amount,
-- pca.budget_currency,
-- pca.cso_contribution,
-- pca.frs_total_frs_amt,
-- pca.frs_total_intervention_amt,
-- pca.frs_total_outstanding_amt,
-- pca.location_p_codes,
-- pca.total_budget,
-- pca.total_unicef_budget,
-- pca.unicef_cash,
-- pca.planned_budget,
-- pca.multi_curr_flag,
-- pca.fr_currency,
-- pca.all_currencies_are_consistent,
-- pca.budget_currency,
-- pca.fr_currencies_are_consistent,
pca.section_names[1] section, 
pca.offices_set[1] office, 
'[' || date_part('year', pca.start) || ']' period,
case 
    when pca.status = 'active' then 'yes'  else 'no' 
    end running, 
donors.donor,
donors.value funds
FROM 
  etools_pca pca, 
  jsonb_to_recordset(pca.donors_set) as donors(donor text, value text)
WHERE 
  donors_set != '{}'
  and pca.id IN (IDS)
"""

ALL_MASTER_INDICATORS = """
SELECT a.sequence,
       a.id,
       a.NAME,
       a.awp_code,
       a.awp_target,
       a.ram_result,
       CASE
         WHEN denominator.value > 0 THEN numerator.value / denominator.value
         ELSE 0
       END  AS value,
       CASE
         WHEN denominator.value > 0 THEN numerator.value / denominator.value
         ELSE 0
       END  AS achieved,
       100  AS total_targets,
       0    AS reports_count
FROM   pivoting_masterindicator a,
       (SELECT a.master_id AS master_id,
               b.value     AS value
        FROM   pivoting_mastersubindicator a,
               (SELECT a.id,
                       a.NAME,
                       a.awp_code,
                       a.target,
                       CASE
                         WHEN a.aggregation_method = 'SUM' THEN Sum(b.total_ind)
                         WHEN a.aggregation_method = 'AVG' THEN Avg(b.total_ind)
                       END                  AS value,
                       Sum(b.reports_count) AS reports_count
                FROM   pivoting_subindicator a,
                       (SELECT a.id,
                               a.NAME,
                               Sum(b.indicator_value) AS total_ind,
                               Avg(b.indicator_value) AS avg_ind,
                               Count(*)               AS reports_count
                        FROM   pivoting_indicatornew a,
                               pivoting_activityreportnew b
                        WHERE  a.ai_indicator = b.indicator_id
                               AND a.database_id = b.dbase_id
                               --AND b.funded_by = 'UNICEF'
                               AND b.project_label in (PNUMBERS)
                        GROUP  BY a.id,
                                  a.NAME) b,
                       pivoting_subindicator_indicators c
                WHERE  a.id = c.subindicator_id
                       AND c.indicatornew_id = b.id
                GROUP  BY a.id) b
        WHERE  a.sub_id = b.id
               AND a.effect = 'NUMERATOR') numerator,
       (SELECT a.master_id AS master_id,
               b.value     AS value
        FROM   pivoting_mastersubindicator a,
               (SELECT a.id,
                       a.NAME,
                       a.awp_code,
                       a.target,
                       CASE
                         WHEN a.aggregation_method = 'SUM' THEN Sum(b.total_ind)
                         WHEN a.aggregation_method = 'AVG' THEN Avg(b.total_ind)
                       END                  AS value,
                       Sum(b.reports_count) AS reports_count
                FROM   pivoting_subindicator a,
                       (SELECT a.id,
                               a.NAME,
                               Sum(b.indicator_value) AS total_ind,
                               Avg(b.indicator_value) AS avg_ind,
                               Count(*)               AS reports_count
                        FROM   pivoting_indicatornew a,
                               pivoting_activityreportnew b
                        WHERE  a.ai_indicator = b.indicator_id
                               AND a.database_id = b.dbase_id
                               --AND b.funded_by = 'UNICEF'
                               AND b.project_label in (PNUMBERS)
                        GROUP  BY a.id,
                                  a.NAME) b,
                       pivoting_subindicator_indicators c
                WHERE  a.id = c.subindicator_id
                       AND c.indicatornew_id = b.id
                       GROUP  BY a.id) b
        WHERE  a.sub_id = b.id
               AND a.effect = 'DENOMINATOR') denominator
WHERE  a.id = numerator.master_id
       AND a.id = denominator.master_id
       AND a.database_id IN (IDS)
UNION
SELECT a.sequence,
       a.id,
       a.NAME,
       a.awp_code,
       a.awp_target,
       a.ram_result,
       b.value,
       b.achieved,
       b.total_targets,
       b.reports_count
FROM   pivoting_masterindicator a
       LEFT JOIN (SELECT a.id,
                         CASE
                           WHEN a.aggregation_method = 'SUM' THEN Sum(b.value)
                           WHEN a.aggregation_method = 'AVERAGE' THEN
                           Avg(b.value)
                           WHEN a.aggregation_method = 'MINIMUM' THEN
                           Min(b.value)
                           WHEN a.aggregation_method = 'MAXIMUM' THEN
                           Max(b.value)
                         END                               AS value,
                         -- awp_target could be zero
                         CASE
                           WHEN a.awp_target > 0 THEN Sum(b.value) * 100 / a.awp_target
                           ELSE 0
                         END AS achieved,
                         Sum(b.target)                     AS total_targets,
                         Sum(b.reports_count)              AS reports_count
                  FROM   pivoting_masterindicator a,
                         (SELECT a.id,
                                 a.NAME,
                                 a.awp_code,
                                 a.target,
                                 CASE
                                   WHEN a.aggregation_method = 'SUM' THEN
                                   Sum(b.total_ind)
                                   WHEN a.aggregation_method = 'AVG' THEN
                                   Avg(b.total_ind)
                                 END                  AS value,
                                 Sum(b.reports_count) AS reports_count
                          FROM   pivoting_subindicator a,
                                 (SELECT a.id,
                                         a.NAME,
                                         Sum(b.indicator_value) AS total_ind,
                                         Avg(b.indicator_value) AS avg_ind,
                                         Count(*)               AS reports_count
                                  FROM   pivoting_indicatornew a,
                                         pivoting_activityreportnew b
                                  WHERE  a.ai_indicator = b.indicator_id
                                         AND a.database_id = b.dbase_id
                                         --AND b.funded_by = 'UNICEF'
                                         AND b.project_label in (PNUMBERS)
                                  GROUP  BY a.id,
                                            a.NAME) b,
                                 pivoting_subindicator_indicators c
                          WHERE  a.id = c.subindicator_id
                                 AND c.indicatornew_id = b.id
                          GROUP  BY a.id) b,
                         pivoting_mastersubindicator c
                  WHERE  a.id = c.master_id
                         AND a.database_id IN (IDS)
                         AND c.sub_id = b.id
                         AND a.aggregation_method IN ( 'SUM', 'AVERAGE', 'MINIMUM', 'MAXIMUM' )
                         AND c.effect = 'TOTAL'
                  GROUP  BY a.id) b
              ON a.id = b.id
WHERE a.database_id IN (IDS)
"""

PCAS_MASTER_INDICATORS_old = """

SELECT a.sequence,
       a.id,
       a.NAME,
       a.awp_code,
       a.awp_target,
       a.ram_result,
	   numerator.project_label,
       CASE
         WHEN denominator.value > 0 THEN numerator.value / denominator.value
         ELSE 0
       END  AS value,
       CASE
         WHEN denominator.value > 0 THEN numerator.value / denominator.value
         ELSE 0
       END  AS achieved,
       100  AS total_targets,
       0    AS reports_count
FROM   pivoting_masterindicator a,
       (SELECT a.master_id AS master_id,
               b.value     AS value,
				b.project_label
        FROM   pivoting_mastersubindicator a,
               (SELECT a.id,
                       a.NAME,
                       a.awp_code,
                       a.target,
						b.project_label,
                       CASE
                         WHEN a.aggregation_method = 'SUM' THEN Sum(b.total_ind)
                         WHEN a.aggregation_method = 'AVG' THEN Avg(b.total_ind)
                       END                  AS value,
                       Sum(b.reports_count) AS reports_count
                FROM   pivoting_subindicator a,
                       (SELECT a.id,
                               a.NAME,
								b.project_label,
                               Sum(b.indicator_value) AS total_ind,
                               Avg(b.indicator_value) AS avg_ind,
                               Count(*)               AS reports_count
                        FROM   pivoting_indicatornew a,
                               pivoting_activityreportnew b
                        WHERE  a.ai_indicator = b.indicator_id
                               AND a.database_id = b.dbase_id
                               --AND b.funded_by = 'UNICEF'
                               AND b.project_label in (PNUMBERS)
                        GROUP  BY a.id,
                                  a.NAME, b.project_label) b,
                       pivoting_subindicator_indicators c
                WHERE  a.id = c.subindicator_id
                       AND c.indicatornew_id = b.id
                GROUP  BY a.id, b.project_label) b
        WHERE  a.sub_id = b.id
               AND a.effect = 'NUMERATOR') numerator,
       (SELECT a.master_id AS master_id,
		b.project_label,
               b.value     AS value
        FROM   pivoting_mastersubindicator a,
               (SELECT a.id,
                       a.NAME,
				b.project_label,
                       a.awp_code,
                       a.target,
                       CASE
                         WHEN a.aggregation_method = 'SUM' THEN Sum(b.total_ind)
                         WHEN a.aggregation_method = 'AVG' THEN Avg(b.total_ind)
                       END                  AS value,
                       Sum(b.reports_count) AS reports_count
                FROM   pivoting_subindicator a,
                       (SELECT a.id,
                               a.NAME,
						b.project_label,
                               Sum(b.indicator_value) AS total_ind,
                               Avg(b.indicator_value) AS avg_ind,
                               Count(*)               AS reports_count
                        FROM   pivoting_indicatornew a,
                               pivoting_activityreportnew b
                        WHERE  a.ai_indicator = b.indicator_id
                               AND a.database_id = b.dbase_id
                               --AND b.funded_by = 'UNICEF'
                               AND b.project_label in (PNUMBERS)
                        GROUP  BY a.id,
                                  a.NAME, b.project_label) b,
                       pivoting_subindicator_indicators c
                WHERE  a.id = c.subindicator_id
                       AND c.indicatornew_id = b.id
                       GROUP  BY a.id,b.project_label) b
        WHERE  a.sub_id = b.id
               AND a.effect = 'DENOMINATOR') denominator
WHERE  a.id = numerator.master_id
       AND a.id = denominator.master_id
       --AND a.database_id IN (IDS)

UNION
SELECT a.sequence,
       a.id,
	   a.NAME,
       a.awp_code,
       a.awp_target,
       a.ram_result,
	   b.project_label,
       b.value,
       b.achieved,
       b.total_targets,
       b.reports_count
FROM   pivoting_masterindicator a
       LEFT JOIN (SELECT a.id, b.project_label,
                         CASE
                           WHEN a.aggregation_method = 'SUM' THEN Sum(b.value)
                           WHEN a.aggregation_method = 'AVERAGE' THEN
                           Avg(b.value)
                           WHEN a.aggregation_method = 'MINIMUM' THEN
                           Min(b.value)
                           WHEN a.aggregation_method = 'MAXIMUM' THEN
                           Max(b.value)
                         END                               AS value,
                         -- awp_target could be zero
                         CASE
                           WHEN a.awp_target > 0 THEN Sum(b.value) * 100 / a.awp_target
                           ELSE 0
                         END AS achieved,
                         Sum(b.target)                     AS total_targets,
                         Sum(b.reports_count)              AS reports_count
                  FROM   pivoting_masterindicator a,
                         (SELECT a.id,
                                 a.NAME,
						  		b.project_label,
                                 a.awp_code,
                                 a.target,
                                 CASE
                                   WHEN a.aggregation_method = 'SUM' THEN
                                   Sum(b.total_ind)
                                   WHEN a.aggregation_method = 'AVG' THEN
                                   Avg(b.total_ind)
                                 END                  AS value,
                                 Sum(b.reports_count) AS reports_count
                          FROM   pivoting_subindicator a,
                                 (SELECT a.id,
                                         a.NAME,
								  			b.project_label,
                                         Sum(b.indicator_value) AS total_ind,
                                         Avg(b.indicator_value) AS avg_ind,
                                         Count(*)               AS reports_count
                                  FROM   pivoting_indicatornew a,
                                         pivoting_activityreportnew b
                                  WHERE  a.ai_indicator = b.indicator_id
                                         AND a.database_id = b.dbase_id
                                         --AND b.funded_by = 'UNICEF'
                                         AND b.project_label in (PNUMBERS)
                                  GROUP  BY a.id,
                                            a.NAME, b.project_label) b,
                                 pivoting_subindicator_indicators c
                          WHERE  a.id = c.subindicator_id
                                 AND c.indicatornew_id = b.id
                          GROUP  BY a.id, b.project_label) b,
                         pivoting_mastersubindicator c
                  WHERE  a.id = c.master_id
                         --AND a.database_id IN (IDS)
                         AND c.sub_id = b.id
                         AND a.aggregation_method IN ( 'SUM', 'AVERAGE', 'MINIMUM', 'MAXIMUM' )
                         AND c.effect = 'TOTAL'
                  GROUP  BY a.id, b.project_label) b
              ON a.id = b.id
--WHERE a.database_id IN (IDS)
"""

PCAS_MASTER_INDICATORS = """

SELECT
  f.id,
  f.NAME,
  b.project_label,
  f.awp_code,
  f.awp_target,
  Sum(b.indicator_value) AS value,
  CASE
    WHEN f.awp_target > 0 THEN Sum(b.indicator_value) * 100 / f.awp_target
    ELSE 0
  END AS achieved,
  Count(*) AS reports_count
FROM
  pivoting_indicatornew a,
  pivoting_activityreportnew b,
  pivoting_subindicator_indicators c,
  pivoting_subindicator d,
  pivoting_mastersubindicator e,
  pivoting_masterindicator f
WHERE
  a.ai_indicator = b.indicator_id
  AND a.database_id = b.dbase_id
  --AND b.funded_by = 'UNICEF'
  AND b.project_label in (PNUMBERS)
  AND c.indicatornew_id = a.id
  AND d.id = c.subindicator_id
  AND e.sub_id = d.id
  AND e.master_id = f.id
GROUP BY
  f.id,
  f.NAME,
  b.project_label,
  f.awp_code,
  f.awp_target,
  d.aggregation_method
"""

PCA_SUMMARY_PARTNERS = """
select
partner_name, count(distinct number) pds, 
sum(total_unicef_budget::numeric) total_unicef_budget, 
sum(total_budget::numeric) total_budget, 
sum(actual_amount::numeric) actual_amount, 
sum(donors.value::numeric) donations
from (select
*, case when donors_set = '{}' then '[{}]'
else donors_set END as donors_set_a
	from etools_pca
	where status in (STATUSR) and
partner_name != '' and
total_unicef_budget::numeric > 0     
) etools_pca, jsonb_to_recordset(donors_set_a) as donors(donor text, value text) 
group by partner_name
"""

PCA_SUMMARY_SECTIONS = """
select 
section_names[1], count(distinct number) pds, 
sum(total_unicef_budget::numeric) total_unicef_budget, 
sum(total_budget::numeric) total_budget, 
sum(actual_amount::numeric) actual_amount, 
sum(donors.value::numeric) donations
from (select
*, case when donors_set = '{}' then '[{}]'
else donors_set END as donors_set_a
	from etools_pca
	where status in (STATUSR) and
section_names != '{}' and
array_length(section_names, 1) = 1 and
total_unicef_budget::numeric > 0     
) etools_pca, jsonb_to_recordset(donors_set_a) as donors(donor text, value text) 
group by section_names
"""

PCA_ENDING_SOON = """
select "end" - current_date as days_to_end, *  from etools_pca where status = 'active' and "end" >= NOW() and "end" <= NOW() + interval '30' day
"""

ACTIVITYINFO_SUMMARY = """
select distinct
  b.name as "Section",
  c.year as "Reporting_Year",
  a.location_adminlevel_governorate as "Governorate",
  a.location_adminlevel_caza as "District",
  --location_adminlevel_caza_code as "District Code",
  a.location_name as "Gateway_Name",
  a.location_adminlevel_cadastral_area as "Cadaster",
  a.location_adminlevel_cadastral_area_code as "CAS_CODE",
  a.location_alternate_name as "P_CODE",
  a.partner_description as "Partner_Full_Name",
  a.partner_label as "Partner",
  a.location_latitude as "Latitude",
  a.location_longitude as "Longitude",
  a.database_ai_id as "NeuroDB_ID",
  a.emergency as "Emergency",
  a.project_plan as "Project_Plan",
  a.project as "Project"
  ,a.indicator_name as "Quantity field"
from
  pivoting_activityreportnew a,
  pivoting_database b,
  pivoting_reportingyear c
where
  a.dbase_id = b.id
  and b.reporting_year_id = c.id
  and (c.year = TO_CHAR(CURRENT_DATE, 'YYYY')  -- current year
   OR c.year = TO_CHAR(CURRENT_DATE - INTERVAL '1 year', 'YYYY'))
"""

ACTIVITYINFO_PCA_SUMMARY = """
select distinct
  a.database_ai_id as "NeuroDB_ID",
  c.year as "Reporting_Year",
  b.name as "Section",
  a.location_adminlevel_governorate as "Governorate",
  a.location_adminlevel_caza as "District",
  a.location_name as "Gateway_Name",
  a.location_adminlevel_cadastral_area as "Cadaster",
  a.location_adminlevel_cadastral_area_code as "CAS_CODE",
  a.location_alternate_name as "P_CODE",
  a.location_latitude as "Latitude",
  a.location_longitude as "Longitude",
  a.partner_description as "Partner_Full_Name",
  a.partner_label as "Partner",
  d.number as "PD_Reference",
    json_data->>'donor' AS "Donor",
    json_data->>'grant_number' AS "Grant",
    json_data->>'value' AS "Amount in $",
  a.emergency as "Emergency",
  a.project_plan as "Project_Plan",
  a.project as "Project",
    json_data->>'donor_code' AS "Donor_Code"
from
  pivoting_activityreportnew a,
  pivoting_database b,
  pivoting_reportingyear c,
  etools_pca d,
  jsonb_array_elements(donors_set) AS json_data
where
  a.dbase_id = b.id
  and b.reporting_year_id = c.id
  and split_part(d.number, '-', 1) = split_part(a.project_label, '-', 1)
  --and a.funded_by = 'UNICEF'
  and jsonb_typeof(donors_set) = 'array'
  and (c.year = TO_CHAR(CURRENT_DATE, 'YYYY')  -- current year
   OR c.year = TO_CHAR(CURRENT_DATE - INTERVAL '1 year', 'YYYY'))
"""

ETOOLS_LOCATIONS = """select
  a.short_name AS "Partner_Short_Name",
  a.partner_name AS "Partner_Full_Name",
  a.number AS "PD_Number",
  a.status AS "Status",
  a.start AS "Start_Date",
  a.end AS "End_Date",
  a.section_name AS "Section",
  a.location_code AS "Location_Code",
  b.p_code AS "P_CODE",
  b.latitude AS "Latitude",
  b.longitude AS "Longitude",
  b.cas_code AS "CAS_CODE",
  b.location AS "Location",
  b.cadaster AS "Cadaster",
  b.district AS "District",
  b.governorate AS "Governorate"
from
  (
    select
      b.short_name,
      a.partner_name,
      a.number,
      a.status,
      a.start,
      a.end,
      a.section_name,
      a.location_code
    from
      (
        SELECT DISTINCT
          a.*,
          unnest(a.section_names) AS section_name
        from
          (
            SELECT DISTINCT
              *,
              unnest(location_p_codes) AS location_code
            FROM
              etools_pca
          ) a
      ) a,
      etools_partnerorganization b
    where
      a.partner_id = b.id
  ) a
  left join (
    select
      b.p_code,
      b.latitude,
      b.longitude,
      b.cas_code,
      b.name AS location,
      c.name AS cadaster,
      d.name AS district,
      e.name AS governorate
    from
      pivoting_simplelocation b,
      pivoting_cadasterlocation c,
      pivoting_districtlocation d,
      pivoting_governoratelocation e
    where
      b.cas_code = c.code
      and c.dist_code = d.code
      and d.gov_code = e.code
  ) b ON a.location_code = b.p_code;
  """