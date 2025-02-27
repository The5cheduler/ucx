import logging
import os
import re
import typing
from dataclasses import dataclass

from databricks.sdk import WorkspaceClient

from databricks.labs.ucx.framework.crawlers import CrawlerBase, SqlBackend
from databricks.labs.ucx.hive_metastore.mounts import Mounts
from databricks.labs.ucx.mixins.sql import Row

logger = logging.getLogger(__name__)


@dataclass
class ExternalLocation:
    location: str


class ExternalLocationCrawler(CrawlerBase):
    _prefix_size: typing.ClassVar[list[int]] = [1, 12]

    def __init__(self, ws: WorkspaceClient, sbe: SqlBackend, schema):
        super().__init__(sbe, "hive_metastore", schema, "external_locations", ExternalLocation)
        self._ws = ws

    def _external_locations(self, tables: list[Row], mounts) -> list[ExternalLocation]:
        min_slash = 2
        external_locations: list[ExternalLocation] = []
        for table in tables:
            location = table.location
            if location is not None and len(location) > 0:
                if location.startswith("dbfs:/mnt"):
                    for mount in mounts:
                        if location[5:].startswith(mount.name):
                            location = location[5:].replace(mount.name, mount.source)
                            break
                if (
                    not location.startswith("dbfs")
                    and (self._prefix_size[0] < location.find(":/") < self._prefix_size[1])
                    and not location.startswith("jdbc")
                ):
                    dupe = False
                    loc = 0
                    while loc < len(external_locations) and not dupe:
                        common = (
                            os.path.commonpath(
                                [external_locations[loc].location, os.path.dirname(location) + "/"]
                            ).replace(":/", "://")
                            + "/"
                        )
                        if common.count("/") > min_slash:
                            external_locations[loc] = ExternalLocation(common)
                            dupe = True
                        loc += 1
                    if not dupe:
                        external_locations.append(ExternalLocation(os.path.dirname(location) + "/"))
                if location.startswith("jdbc"):
                    pattern = r"(\w+)=(.*?)(?=\s*,|\s*\])"

                    # Find all matches in the input string
                    # Storage properties is of the format
                    # "[personalAccessToken=*********(redacted), \
                    #  httpPath=/sql/1.0/warehouses/65b52fb5bd86a7be, host=dbc-test1-aa11.cloud.databricks.com, \
                    #  dbtable=samples.nyctaxi.trips]"
                    matches = re.findall(pattern, table.storage_properties)

                    # Create a dictionary from the matches
                    result_dict = dict(matches)

                    # Fetch the value of host from the newly created dict
                    host = result_dict.get("host", "")
                    port = result_dict.get("port", "")
                    database = result_dict.get("database", "")
                    httppath = result_dict.get("httpPath", "")
                    provider = result_dict.get("provider", "")
                    # dbtable = result_dict.get("dbtable", "")

                    # currently supporting databricks and mysql external tables
                    # add other jdbc types
                    if "databricks" in location.lower():
                        jdbc_location = f"jdbc:databricks://{host};httpPath={httppath}"
                    elif "mysql" in location.lower():
                        jdbc_location = f"jdbc:mysql://{host}:{port}/{database}"
                    elif not provider == "":
                        jdbc_location = f"jdbc:{provider.lower()}://{host}:{port}/{database}"
                    else:
                        jdbc_location = f"{location.lower()}/{host}:{port}/{database}"
                    external_locations.append(ExternalLocation(jdbc_location))

        return external_locations

    def _external_location_list(self):
        tables = list(
            self._backend.fetch(
                f"SELECT location, storage_properties FROM {self._schema}.tables WHERE location IS NOT NULL"
            )
        )
        mounts = Mounts(self._backend, self._ws, self._schema).snapshot()
        return self._external_locations(list(tables), list(mounts))

    def snapshot(self) -> list[ExternalLocation]:
        return self._snapshot(self._try_fetch, self._external_location_list)

    def _try_fetch(self) -> list[ExternalLocation]:
        for row in self._fetch(f"SELECT * FROM {self._schema}.{self._table}"):
            yield ExternalLocation(*row)
