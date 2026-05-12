# Collection And Thresholds Reference

This document lists what CTERA Monitoring Dashboard collects for each dataset and the threshold rules currently configured for dashboard warning and critical states.

## Summary

| Dataset | Fields Collected | Fields With Thresholds | Source Files |
|---|---:|---:|---|
| Filers | 31 | 6 | ctera_collect.py, thresholds.yaml |
| Tenants | 9 | 1 | pg_collect_tenants.py, thresholds.yaml |
| Portal Servers | 4 | 1 | ctera_collect.py, thresholds.yaml |
| Storage Nodes | 6 | 0 | ctera_collect.py, thresholds.yaml |
| Portal Tasks | 9 | 2 | ctera_collect.py, thresholds.yaml |
| Licenses | 31 | 0 | pg_healthcheck.py |
| PG Long Queries | 17 | 1 | pg_healthcheck.py, thresholds.yaml |
| PG Wraparound DB | 9 | 1 | pg_healthcheck.py, thresholds.yaml |
| PG Wraparound Top Tables | 11 | 2 | pg_healthcheck.py, thresholds.yaml |
| PG Wraparound Summary | 8 | 2 | pg_healthcheck.py, thresholds.yaml |
| PG Vacuum Analyze | 17 | 1 | pg_healthcheck.py, thresholds.yaml |
| PG Table Sizes | 11 | 0 | pg_healthcheck.py |
| PG Table Bloat | 16 | 1 | pg_healthcheck.py, thresholds.yaml |
| PG Index Bloat | 16 | 1 | pg_healthcheck.py, thresholds.yaml |
| Server Metrics | 28 | 8 | ssh_collect_from_pg.py, thresholds.yaml |
| Nomad Nodes | 15 | 0 | ssh_collect_from_pg.py |
| Consul Members | 14 | 0 | ssh_collect_from_pg.py |
| Docker Containers | 18 | 4 | ssh_collect_from_pg.py, thresholds.yaml |

## Notes

- Storage nodes currently have no threshold rules in `thresholds.yaml`.
- Licenses are collected and displayed, but the dashboard's license severity handling is mostly code-driven rather than configured in `thresholds.yaml`.
- Portal tasks filter out ignored task names before display.
- Server health includes separate Docker thresholds in addition to the main server-metrics thresholds.

## Filers

| Field | Collected | Warn Threshold | Crit Threshold | Notes |
|---|---|---|---|---|
| Tenant | Yes |  |  |  |
| Filer Name | Yes |  |  |  |
| CloudSync Status | Yes | eq UploadIsStalled | eq NoFolder |  |
| selfScanIntervalInHours | Yes |  |  |  |
| uploadingFiles | Yes | gt 10000 | gt 30000 |  |
| scanningFiles | Yes |  |  |  |
| selfVerificationscanningFiles | Yes |  |  |  |
| MetaLogsSetting | Yes |  |  |  |
| AuditLogsStatus | Yes |  |  |  |
| DeviceLocation | Yes |  |  |  |
| AuditLogsPath | Yes |  |  |  |
| MetaLogMaxSize | Yes |  |  |  |
| MetaLogMaxFiles | Yes |  |  |  |
| CurrentFirmware | Yes |  |  |  |
| License | Yes |  |  |  |
| EvictionPercentage | Yes |  |  |  |
| CurrentVolumeStorage | Yes |  |  |  |
| SN | Yes |  |  |  |
| MAC | Yes |  |  |  |
| IP Config | Yes |  |  |  |
| DNS Server1 | Yes |  |  |  |
| DNS Server2 | Yes |  |  |  |
| AD Domain Status | Yes | ne Ok |  |  |
| AD Mapping | Yes |  |  |  |
| Alerts | Yes | ge 10 | ge 20 |  |
| TimeServer | Yes |  |  |  |
| uptime | Yes |  |  |  |
| Current Performance | Yes |  |  | Derived display column containing current CPU and memory together. |
| Max CPU | Yes | ge 80 | ge 95 |  |
| Max Memory | Yes | ge 80 | ge 90 |  |
| DB Size | Yes |  |  | Collected via telnet-enabled shell command against CloudSync.db. |

Source files: `ctera_collect.py, thresholds.yaml`

## Tenants

| Field | Collected | Warn Threshold | Crit Threshold | Notes |
|---|---|---|---|---|
| UID | Yes |  |  |  |
| Tenant | Yes |  |  |  |
| PortalType | Yes |  |  |  |
| EnableResellerProvisioning | Yes |  |  |  |
| Active | Yes |  |  |  |
| Deleted | Yes | eq True |  |  |
| CreatedDate | Yes |  |  |  |
| DeletedDate | Yes |  |  |  |
| PlanName | Yes |  |  |  |

Source files: `pg_collect_tenants.py, thresholds.yaml`

## Portal Servers

| Field | Collected | Warn Threshold | Crit Threshold | Notes |
|---|---|---|---|---|
| Name | Yes |  |  |  |
| Connected | Yes |  | eq False |  |
| IsApplicationServer | Yes |  |  |  |
| IsMainDB | Yes |  |  |  |

Source files: `ctera_collect.py, thresholds.yaml`

## Storage Nodes

No storage thresholds are configured in thresholds.yaml.

| Field | Collected | Warn Threshold | Crit Threshold | Notes |
|---|---|---|---|---|
| Name | Yes |  |  |  |
| Driver | Yes |  |  |  |
| Bucket | Yes |  |  |  |
| ReadOnly | Yes |  |  |  |
| DedicatedTo | Yes |  |  |  |
| DirectIO | Yes |  |  |  |

Source files: `ctera_collect.py, thresholds.yaml`

## Portal Tasks

| Field | Collected | Warn Threshold | Crit Threshold | Notes |
|---|---|---|---|---|
| ServerName | Yes |  |  |  |
| TaskName | Yes |  |  | Rows with ignored task names are filtered from the dashboard before display. |
| Enabled | Yes |  |  |  |
| State | Yes |  | eq failed |  |
| StartTime | Yes |  |  |  |
| EndTime | Yes |  |  |  |
| ElapsedSeconds | Yes | ge 7200 | ge 43200 |  |
| Message | Yes |  |  |  |
| TaskID | Yes |  |  |  |
| (ignored task names) | No |  |  | CSRRequestsProcessor, Antivirus background scanning - MainDB |

Source files: `ctera_collect.py, thresholds.yaml`

## Licenses

Dashboard currently flags expired/invalid licenses in code, but there is no licenses section in thresholds.yaml.

| Field | Collected | Warn Threshold | Crit Threshold | Notes |
|---|---|---|---|---|
| db_id | Yes |  |  |  |
| index | Yes |  |  |  |
| key | Yes |  |  |  |
| original_key | Yes |  |  |  |
| expired | Yes |  |  |  |
| expiration_date | Yes |  |  |  |
| appliances | Yes |  |  |  |
| server_agents | Yes |  |  |  |
| workstation_agents | Yes |  |  |  |
| cloud_drives | Yes |  |  |  |
| cloud_drives_lite | Yes |  |  |  |
| valid | Yes |  |  |  |
| antivirus | Yes |  |  |  |
| varonis | Yes |  |  |  |
| key_manager | Yes |  |  |  |
| dlp | Yes |  |  |  |
| portal_license | Yes |  |  |  |
| vgateways4 | Yes |  |  |  |
| vgateways8 | Yes |  |  |  |
| vgateways32 | Yes |  |  |  |
| vgateways64 | Yes |  |  |  |
| vgateways128 | Yes |  |  |  |
| vgateways256 | Yes |  |  |  |
| storage | Yes |  |  |  |
| comment | Yes |  |  |  |
| global_file_lock | Yes |  |  |  |
| pg_host | Yes |  |  |  |
| pg_port | Yes |  |  |  |
| pg_db | Yes |  |  |  |
| cluster | Yes |  |  |  |
| collected_at | Yes |  |  |  |

Source files: `pg_healthcheck.py`

## PG Long Queries

| Field | Collected | Warn Threshold | Crit Threshold | Notes |
|---|---|---|---|---|
| pid | Yes |  |  |  |
| usename | Yes |  |  |  |
| application_name | Yes |  |  |  |
| client_addr | Yes |  |  |  |
| state | Yes |  |  |  |
| wait_event_type | Yes |  |  |  |
| wait_event | Yes |  |  |  |
| backend_start | Yes |  |  |  |
| xact_start | Yes |  |  |  |
| query_start | Yes |  |  |  |
| runtime | Yes | ge 30 | ge 60 |  |
| query | Yes |  |  |  |
| pg_host | Yes |  |  |  |
| pg_port | Yes |  |  |  |
| pg_db | Yes |  |  |  |
| cluster | Yes |  |  |  |
| collected_at | Yes |  |  |  |

Source files: `pg_healthcheck.py, thresholds.yaml`

## PG Wraparound DB

| Field | Collected | Warn Threshold | Crit Threshold | Notes |
|---|---|---|---|---|
| datname | Yes |  |  |  |
| age | Yes |  |  |  |
| autovacuum_freeze_max_age | Yes |  |  |  |
| pct_of_max | Yes | ge 70 | ge 85 |  |
| pg_host | Yes |  |  |  |
| pg_port | Yes |  |  |  |
| pg_db | Yes |  |  |  |
| cluster | Yes |  |  |  |
| collected_at | Yes |  |  |  |

Source files: `pg_healthcheck.py, thresholds.yaml`

## PG Wraparound Top Tables

| Field | Collected | Warn Threshold | Crit Threshold | Notes |
|---|---|---|---|---|
| schema | Yes |  |  |  |
| table | Yes |  |  |  |
| age | Yes |  |  |  |
| autovacuum_freeze_max_age | Yes |  |  |  |
| pct_of_max | Yes | ge 70 | ge 85 |  |
| size_bytes | Yes | gt 10737418240 (10 GiB) | gt 53687091200 (50 GiB) |  |
| pg_host | Yes |  |  |  |
| pg_port | Yes |  |  |  |
| pg_db | Yes |  |  |  |
| cluster | Yes |  |  |  |
| collected_at | Yes |  |  |  |

Source files: `pg_healthcheck.py, thresholds.yaml`

## PG Wraparound Summary

| Field | Collected | Warn Threshold | Crit Threshold | Notes |
|---|---|---|---|---|
| oldest_current_xid | Yes |  |  |  |
| percent_towards_wraparound | Yes | ge 70 | ge 85 |  |
| percent_towards_emergency_autovac | Yes | ge 80 | ge 90 |  |
| pg_host | Yes |  |  |  |
| pg_port | Yes |  |  |  |
| pg_db | Yes |  |  |  |
| cluster | Yes |  |  |  |
| collected_at | Yes |  |  |  |

Source files: `pg_healthcheck.py, thresholds.yaml`

## PG Vacuum Analyze

| Field | Collected | Warn Threshold | Crit Threshold | Notes |
|---|---|---|---|---|
| schema | Yes |  |  |  |
| table | Yes |  |  |  |
| n_live_tup | Yes |  |  |  |
| n_dead_tup | Yes | ge 1000000 | ge 5000000 |  |
| last_vacuum | Yes |  |  |  |
| last_autovacuum | Yes |  |  |  |
| last_analyze | Yes |  |  |  |
| last_autoanalyze | Yes |  |  |  |
| vacuum_count | Yes |  |  |  |
| autovacuum_count | Yes |  |  |  |
| analyze_count | Yes |  |  |  |
| autoanalyze_count | Yes |  |  |  |
| pg_host | Yes |  |  |  |
| pg_port | Yes |  |  |  |
| pg_db | Yes |  |  |  |
| cluster | Yes |  |  |  |
| collected_at | Yes |  |  |  |

Source files: `pg_healthcheck.py, thresholds.yaml`

## PG Table Sizes

Collected for visibility only; no thresholds are configured in thresholds.yaml.

| Field | Collected | Warn Threshold | Crit Threshold | Notes |
|---|---|---|---|---|
| schema | Yes |  |  |  |
| table | Yes |  |  |  |
| table_bytes | Yes |  |  |  |
| indexes_bytes | Yes |  |  |  |
| toast_and_other_bytes | Yes |  |  |  |
| total_bytes | Yes |  |  |  |
| pg_host | Yes |  |  |  |
| pg_port | Yes |  |  |  |
| pg_db | Yes |  |  |  |
| cluster | Yes |  |  |  |
| collected_at | Yes |  |  |  |

Source files: `pg_healthcheck.py`

## PG Table Bloat

| Field | Collected | Warn Threshold | Crit Threshold | Notes |
|---|---|---|---|---|
| current_database | Yes |  |  |  |
| schemaname | Yes |  |  |  |
| tblname | Yes |  |  |  |
| real_size | Yes |  |  |  |
| real_size_bytes | Yes |  |  |  |
| extra_size | Yes |  |  |  |
| extra_ratio | Yes |  |  |  |
| fillfactor | Yes |  |  |  |
| bloat_size | Yes | gt 536870912 (512 MiB) | gt 2147483648 (2 GiB) |  |
| bloat_ratio | Yes |  |  |  |
| is_na | Yes |  |  |  |
| pg_host | Yes |  |  |  |
| pg_port | Yes |  |  |  |
| pg_db | Yes |  |  |  |
| cluster | Yes |  |  |  |
| collected_at | Yes |  |  |  |

Source files: `pg_healthcheck.py, thresholds.yaml`

## PG Index Bloat

| Field | Collected | Warn Threshold | Crit Threshold | Notes |
|---|---|---|---|---|
| schemaname | Yes |  |  |  |
| tblname | Yes |  |  |  |
| idxname | Yes |  |  |  |
| real_size | Yes |  |  |  |
| real_size_bytes | Yes |  |  |  |
| extra_size | Yes |  |  |  |
| extra_ratio | Yes |  |  |  |
| fillfactor | Yes |  |  |  |
| bloat_size | Yes | gt 268435456 (256 MiB) | gt 1073741824 (1 GiB) |  |
| bloat_ratio | Yes |  |  |  |
| is_na | Yes |  |  |  |
| pg_host | Yes |  |  |  |
| pg_port | Yes |  |  |  |
| pg_db | Yes |  |  |  |
| cluster | Yes |  |  |  |
| collected_at | Yes |  |  |  |

Source files: `pg_healthcheck.py, thresholds.yaml`

## Server Metrics

| Field | Collected | Warn Threshold | Crit Threshold | Notes |
|---|---|---|---|---|
| Name | Yes |  |  |  |
| Host | Yes |  |  |  |
| Status | Yes |  |  |  |
| UID | Yes |  |  |  |
| Connected | Yes |  | eq False |  |
| MainDB | Yes |  |  |  |
| RunningVersion | Yes |  |  |  |
| PublicIP | Yes |  |  |  |
| UptimeSeconds | Yes |  |  |  |
| Load1 | Yes | ge 8 | ge 16 |  |
| Load5 | Yes |  |  |  |
| Load15 | Yes |  |  |  |
| MemTotalGB | Yes |  |  |  |
| MemUsedGB | Yes |  |  |  |
| MemUsedPct | Yes | ge 85 | ge 92 |  |
| RootDiskSizeGB | Yes |  |  |  |
| RootDiskUsedGB | Yes |  |  |  |
| RootDiskUsedPct | Yes | ge 80 | ge 90 |  |
| DataPoolSizeGB | Yes |  |  |  |
| DataPoolUsedGB | Yes |  |  |  |
| DataPoolUsedPct | Yes | ge 80 | ge 90 |  |
| DBArchivePoolSizeGB | Yes |  |  |  |
| DBArchivePoolUsedGB | Yes |  |  |  |
| DBArchivePoolUsedPct | Yes | ge 80 | ge 90 |  |
| CPUUserPct | Yes |  |  |  |
| CPUSystemPct | Yes |  |  |  |
| CPUIOWaitPct | Yes | ge 15 | ge 30 |  |
| CPUIDLEPct | Yes | le 15 | le 5 |  |

Source files: `ssh_collect_from_pg.py, thresholds.yaml`

## Nomad Nodes

No Nomad-specific thresholds are configured in thresholds.yaml.

| Field | Collected | Warn Threshold | Crit Threshold | Notes |
|---|---|---|---|---|
| SourceName | Yes |  |  |  |
| SourceHost | Yes |  |  |  |
| SourceUID | Yes |  |  |  |
| ViewHash | Yes |  |  |  |
| NodeID | Yes |  |  |  |
| NodePool | Yes |  |  |  |
| DC | Yes |  |  |  |
| Name | Yes |  |  |  |
| Class | Yes |  |  |  |
| Address | Yes |  |  |  |
| Version | Yes |  |  |  |
| Drain | Yes |  |  |  |
| Eligibility | Yes |  |  |  |
| Status | Yes |  |  |  |
| CollectionError | Yes |  |  |  |

Source files: `ssh_collect_from_pg.py`

## Consul Members

No Consul-specific thresholds are configured in thresholds.yaml.

| Field | Collected | Warn Threshold | Crit Threshold | Notes |
|---|---|---|---|---|
| SourceName | Yes |  |  |  |
| SourceHost | Yes |  |  |  |
| SourceUID | Yes |  |  |  |
| ViewHash | Yes |  |  |  |
| Node | Yes |  |  |  |
| Address | Yes |  |  |  |
| Status | Yes |  |  |  |
| Type | Yes |  |  |  |
| Build | Yes |  |  |  |
| Protocol | Yes |  |  |  |
| DC | Yes |  |  |  |
| Partition | Yes |  |  |  |
| Segment | Yes |  |  |  |
| CollectionError | Yes |  |  |  |

Source files: `ssh_collect_from_pg.py`

## Docker Containers

| Field | Collected | Warn Threshold | Crit Threshold | Notes |
|---|---|---|---|---|
| SourceName | Yes |  |  |  |
| SourceHost | Yes |  |  |  |
| SourceUID | Yes |  |  |  |
| HostUptimeSeconds | Yes |  |  |  |
| RecentlyBooted | Yes |  |  |  |
| GraceState | Yes |  |  |  |
| ContainerID | Yes |  |  |  |
| ContainerName | Yes |  |  |  |
| Image | Yes |  |  |  |
| State | Yes |  | ne running |  |
| Health | Yes | eq starting | eq unhealthy |  |
| RestartCount | Yes |  |  |  |
| RestartDelta | Yes | ge 1 | ge 3 |  |
| RestartPolicy | Yes |  |  |  |
| StartedAt | Yes |  |  |  |
| FinishedAt | Yes |  |  |  |
| StatusText | Yes |  |  |  |
| CollectionError | Yes |  | ne  |  |

Source files: `ssh_collect_from_pg.py, thresholds.yaml`
