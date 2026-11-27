SELECT *
FROM source WHERE
workflow_file like '%pr.yml%' or workflow_file like '%qg_cas_build_and_test.yml%'
order by job_started_at
