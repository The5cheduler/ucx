import logging
import os
import sys

from databricks.sdk import WorkspaceClient

from databricks.labs.ucx.assessment.crawlers import (
    AzureServicePrincipalCrawler,
    ClustersCrawler,
    GlobalInitScriptCrawler,
    JobsCrawler,
    PipelinesCrawler,
)
from databricks.labs.ucx.config import WorkspaceConfig
from databricks.labs.ucx.framework.crawlers import RuntimeBackend
from databricks.labs.ucx.framework.tasks import task, trigger
from databricks.labs.ucx.hive_metastore import GrantsCrawler, TablesCrawler
from databricks.labs.ucx.hive_metastore.data_objects import ExternalLocationCrawler
from databricks.labs.ucx.hive_metastore.mounts import Mounts
from databricks.labs.ucx.workspace_access.generic import WorkspaceListing
from databricks.labs.ucx.workspace_access.groups import GroupManager
from databricks.labs.ucx.workspace_access.manager import PermissionManager

logger = logging.getLogger(__name__)


@task("assessment", notebook="hive_metastore/tables.scala")
def crawl_tables(_: WorkspaceConfig):
    """Iterates over all tables in the Hive Metastore of the current workspace and persists their metadata, such
    as _database name_, _table name_, _table type_, _table location_, etc., in the Delta table named
    `$inventory_database.tables`. Note that the `inventory_database` is set in the configuration file. The metadata
    stored is then used in the subsequent tasks and workflows to, for example,  find all Hive Metastore tables that
    cannot easily be migrated to Unity Catalog."""


@task("assessment", job_cluster="tacl")
def setup_tacl(_: WorkspaceConfig):
    """(Optimization) Starts `tacl` job cluster in parallel to crawling tables."""


@task("assessment", depends_on=[crawl_tables, setup_tacl], job_cluster="tacl")
def crawl_grants(cfg: WorkspaceConfig):
    """Scans the previously created Delta table named `$inventory_database.tables` and issues a `SHOW GRANTS`
    statement for every object to retrieve the permissions it has assigned to it. The permissions include information
    such as the _principal_, _action type_, and the _table_ it applies to. This is persisted in the Delta table
    `$inventory_database.grants`. Other, migration related jobs use this inventory table to convert the legacy Table
    ACLs to Unity Catalog  permissions.

    Note: This job runs on a separate cluster (named `tacl`) as it requires the proper configuration to have the Table
    ACLs enabled and available for retrieval."""
    backend = RuntimeBackend()
    tables = TablesCrawler(backend, cfg.inventory_database)
    grants = GrantsCrawler(tables)
    grants.snapshot()


@task("assessment")
def crawl_mounts(cfg: WorkspaceConfig):
    """Defines the scope of the _mount points_ intended for migration into Unity Catalog. As these objects are not
    compatible with the Unity Catalog paradigm, a key component of the migration process involves transferring them
    to Unity Catalog External Locations.

    The assessment involves scanning the workspace to compile a list of all existing mount points and subsequently
    storing this information in the `$inventory.mounts` table. This is crucial for planning the migration."""
    ws = WorkspaceClient(config=cfg.to_databricks_config())
    mounts = Mounts(backend=RuntimeBackend(), ws=ws, inventory_database=cfg.inventory_database)
    mounts.inventorize_mounts()


@task("assessment", depends_on=[crawl_mounts, crawl_tables])
def guess_external_locations(cfg: WorkspaceConfig):
    """Determines the shared path prefixes of all the tables. Specifically, the focus is on identifying locations that
    utilize mount points. The goal is to identify the _external locations_ necessary for a successful migration and
    store this information in the `$inventory.external_locations` table.

    The approach taken in this assessment involves the following steps:
      - Extracting all the locations associated with tables that do not use DBFS directly, but a mount point instead
      - Scanning all these locations to identify folders that can act as shared path prefixes
      - These identified external locations will be created subsequently prior to the actual table migration"""
    ws = WorkspaceClient(config=cfg.to_databricks_config())
    crawler = ExternalLocationCrawler(ws, RuntimeBackend(), cfg.inventory_database)
    crawler.snapshot()


@task("assessment")
def assess_jobs(cfg: WorkspaceConfig):
    """Scans through all the jobs and identifies those that are not compatible with UC. The list of all the jobs is
    stored in the `$inventory.jobs` table.

    It looks for:
      - Clusters with Databricks Runtime (DBR) version earlier than 11.3
      - Clusters using Passthrough Authentication
      - Clusters with incompatible Spark config tags
      - Clusters referencing DBFS locations in one or more config options
    """
    ws = WorkspaceClient(config=cfg.to_databricks_config())
    crawler = JobsCrawler(ws, RuntimeBackend(), cfg.inventory_database)
    crawler.snapshot()


@task("assessment")
def assess_clusters(cfg: WorkspaceConfig):
    """Scan through all the clusters and identifies those that are not compatible with UC. The list of all the clusters
    is stored in the`$inventory.clusters` table.

    It looks for:
      - Clusters with Databricks Runtime (DBR) version earlier than 11.3
      - Clusters using Passthrough Authentication
      - Clusters with incompatible spark config tags
      - Clusters referencing DBFS locations in one or more config options
    """
    ws = WorkspaceClient(config=cfg.to_databricks_config())
    crawler = ClustersCrawler(ws, RuntimeBackend(), cfg.inventory_database)
    crawler.snapshot()


@task("assessment")
def assess_pipelines(cfg: WorkspaceConfig):
    """This module scans through all the Pipelines and identifies those pipelines which has Azure Service Principals
    embedded (who has been given access to the Azure storage accounts via spark configurations) in the pipeline
    configurations.

    It looks for:
      - all the pipelines which has Azure Service Principal embedded in the pipeline configuration

    Subsequently, a list of all the pipelines with matching configurations are stored in the
    `$inventory.pipelines` table."""
    ws = WorkspaceClient(config=cfg.to_databricks_config())
    crawler = PipelinesCrawler(ws, RuntimeBackend(), cfg.inventory_database)
    crawler.snapshot()


@task("assessment")
def assess_azure_service_principals(cfg: WorkspaceConfig):
    """This module scans through all the clusters configurations, cluster policies, job cluster configurations,
    Pipeline configurations, Warehouse configuration and identifies all the Azure Service Principals who has been
    given access to the Azure storage accounts via spark configurations referred in those entities.

    It looks in:
      - all those entities and prepares a list of Azure Service Principal embedded in their configurations

    Subsequently, the list of all the Azure Service Principals referred in those configurations are saved
    in the `$inventory.azure_service_principals` table."""
    ws = WorkspaceClient(config=cfg.to_databricks_config())
    crawler = AzureServicePrincipalCrawler(ws, RuntimeBackend(), cfg.inventory_database)
    crawler.snapshot()


@task("assessment")
def assess_global_init_scripts(cfg: WorkspaceConfig):
    """This module scans through all the global init scripts and identifies if there is an Azure Service Principal
    who has been given access to the Azure storage accounts via spark configurations referred in those scripts.

    It looks in:
      - the list of all the global init scripts are saved in the `$inventory.azure_service_principals` table."""
    ws = WorkspaceClient(config=cfg.to_databricks_config())
    crawler = GlobalInitScriptCrawler(ws, RuntimeBackend(), cfg.inventory_database)
    crawler.snapshot()


@task("assessment")
def workspace_listing(cfg: WorkspaceConfig):
    """Scans the workspace for workspace objects. It recursively list all sub directories
    and compiles a list of directories, notebooks, files, repos and libraries in the workspace.

    It uses multi-threading to parallelize the listing process to speed up execution on big workspaces.
    It accepts starting path as the parameter defaulted to the root path '/'."""
    ws = WorkspaceClient(config=cfg.to_databricks_config())
    crawler = WorkspaceListing(
        ws, RuntimeBackend(), cfg.inventory_database, num_threads=cfg.num_threads, start_path=cfg.workspace_start_path
    )
    crawler.snapshot()


@task("assessment", depends_on=[crawl_grants, workspace_listing])
def crawl_permissions(cfg: WorkspaceConfig):
    """Scans the workspace-local groups and all their permissions. The list is stored in the `$inventory.permissions`
    Delta table.

    This is the first step for the _group migration_ process, which is continued in the `migrate-groups` workflow.
    This step includes preparing Legacy Table ACLs for local group migration."""
    ws = WorkspaceClient(config=cfg.to_databricks_config())
    permission_manager = PermissionManager.factory(
        ws,
        RuntimeBackend(),
        cfg.inventory_database,
        num_threads=cfg.num_threads,
        workspace_start_path=cfg.workspace_start_path,
    )
    permission_manager.cleanup()
    permission_manager.inventorize_permissions()


@task("assessment")
def crawl_groups(cfg: WorkspaceConfig):
    """Scans all groups for the local group migration scope"""
    sql_backend = RuntimeBackend()
    ws = WorkspaceClient(config=cfg.to_databricks_config())
    group_manager = GroupManager(
        sql_backend,
        ws,
        cfg.inventory_database,
        cfg.include_group_names,
        cfg.renamed_group_prefix,
        workspace_group_regex=cfg.workspace_group_regex,
        workspace_group_replace=cfg.workspace_group_replace,
        account_group_regex=cfg.account_group_regex,
        external_id_match=cfg.group_match_by_external_id,
    )
    group_manager.snapshot()


@task(
    "assessment",
    depends_on=[
        crawl_grants,
        crawl_groups,
        crawl_permissions,
        guess_external_locations,
        assess_jobs,
        assess_clusters,
        assess_pipelines,
        assess_azure_service_principals,
        assess_global_init_scripts,
        crawl_tables,
    ],
    dashboard="assessment_main",
)
def assessment_report(_: WorkspaceConfig):
    """Refreshes the assessment dashboard after all previous tasks have been completed. Note that you can access the
    dashboard _before_ all tasks have been completed, but then only already completed information is shown."""


@task("migrate-groups", depends_on=[crawl_groups])
def rename_workspace_local_groups(cfg: WorkspaceConfig):
    """Renames workspace local groups by adding `ucx-renamed-` prefix."""
    sql_backend = RuntimeBackend()
    ws = WorkspaceClient(config=cfg.to_databricks_config())
    group_manager = GroupManager(
        sql_backend,
        ws,
        cfg.inventory_database,
        cfg.include_group_names,
        cfg.renamed_group_prefix,
        workspace_group_regex=cfg.workspace_group_regex,
        workspace_group_replace=cfg.workspace_group_replace,
        account_group_regex=cfg.account_group_regex,
        external_id_match=cfg.group_match_by_external_id,
    )
    group_manager.rename_groups()


@task("migrate-groups", depends_on=[rename_workspace_local_groups])
def reflect_account_groups_on_workspace(cfg: WorkspaceConfig):
    """Adds matching account groups to this workspace. The matching account level group(s) must preexist(s) for this
    step to be successful. This process does not create the account level group(s)."""
    sql_backend = RuntimeBackend()
    ws = WorkspaceClient(config=cfg.to_databricks_config())
    group_manager = GroupManager(
        sql_backend,
        ws,
        cfg.inventory_database,
        cfg.include_group_names,
        cfg.renamed_group_prefix,
        workspace_group_regex=cfg.workspace_group_regex,
        workspace_group_replace=cfg.workspace_group_replace,
        account_group_regex=cfg.account_group_regex,
        external_id_match=cfg.group_match_by_external_id,
    )
    group_manager.reflect_account_groups_on_workspace()


@task("migrate-groups", depends_on=[reflect_account_groups_on_workspace], job_cluster="tacl")
def apply_permissions_to_account_groups(cfg: WorkspaceConfig):
    """Fourth phase of the workspace-local group migration process. It does the following:
      - Assigns the full set of permissions of the original group to the account-level one

    It covers local workspace-local permissions for all entities: Legacy Table ACLs, Entitlements,
    AWS instance profiles, Clusters, Cluster policies, Instance Pools, Databricks SQL warehouses, Delta Live
    Tables, Jobs, MLflow experiments, MLflow registry, SQL Dashboards & Queries, SQL Alerts, Token and Password usage
    permissions, Secret Scopes, Notebooks, Directories, Repos, Files.

    See [interactive tutorial here](https://app.getreprise.com/launch/myM3VNn/)."""
    backend = RuntimeBackend()
    ws = WorkspaceClient(config=cfg.to_databricks_config())
    group_manager = GroupManager(
        backend,
        ws,
        cfg.inventory_database,
        cfg.include_group_names,
        cfg.renamed_group_prefix,
        workspace_group_regex=cfg.workspace_group_regex,
        workspace_group_replace=cfg.workspace_group_replace,
        account_group_regex=cfg.account_group_regex,
        external_id_match=cfg.group_match_by_external_id,
    )

    migration_state = group_manager.get_migration_state()
    if len(migration_state.groups) == 0:
        logger.info("Skipping group migration as no groups were found.")
        return

    permission_manager = PermissionManager.factory(
        ws,
        backend,
        cfg.inventory_database,
        num_threads=cfg.num_threads,
        workspace_start_path=cfg.workspace_start_path,
    )
    permission_manager.apply_group_permissions(migration_state)


@task("remove-workspace-local-backup-groups", depends_on=[apply_permissions_to_account_groups])
def delete_backup_groups(cfg: WorkspaceConfig):
    """Last step of the group migration process. Removes all workspace-level backup groups, along with their
    permissions. Execute this workflow only after you've confirmed that workspace-local migration worked
    successfully for all the groups involved."""
    backend = RuntimeBackend()
    ws = WorkspaceClient(config=cfg.to_databricks_config())
    group_manager = GroupManager(
        backend,
        ws,
        cfg.inventory_database,
        cfg.include_group_names,
        cfg.renamed_group_prefix,
        workspace_group_regex=cfg.workspace_group_regex,
        workspace_group_replace=cfg.workspace_group_replace,
        account_group_regex=cfg.account_group_regex,
        external_id_match=cfg.group_match_by_external_id,
    )
    group_manager.delete_original_workspace_groups()


@task("099-destroy-schema")
def destroy_schema(cfg: WorkspaceConfig):
    """This _clean-up_ workflow allows to removes the `$inventory` database, with all the inventory tables created by
    the previous workflow runs. Use this to reset the entire state and start with the assessment step again."""
    RuntimeBackend().execute(f"DROP DATABASE {cfg.inventory_database} CASCADE")


def main(*argv):
    if len(argv) == 0:
        argv = sys.argv
    trigger(*argv)


if __name__ == "__main__":
    if "DATABRICKS_RUNTIME_VERSION" not in os.environ:
        msg = "Only intended to run in Databricks Runtime"
        raise SystemExit(msg)
    main(*sys.argv)
