SELECT
    job_name,
    COUNT(*) AS failure_count,
    ROUND(AVG(job_duration_sec), 2) AS avg_duration_sec
FROM source
WHERE
    event = 'merge_group'
    AND job_conclusion = 'failure'
    AND workflow_conclusion = 'failure'
GROUP BY job_name
ORDER BY failure_count DESC;
