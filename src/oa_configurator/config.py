"""OA_Configurator's own [tools.oa_configurator] section.
Required to test dialect specific schema primitives, this
package provides. This exists solely so its test suite can get a
genuine, self-provisioned database connection via the same
`omop-config configure` mechanism every consumer uses, rather than
depending on another package's test database entry.
"""

from __future__ import annotations

from typing import Annotated, ClassVar

from .domains.resources.schema import CDMDatabaseConfig
from .package_base import PackageConfigBase
from .refs import RefTo


class OAConfiguratorConfig(PackageConfigBase):
    tool_name: ClassVar[str] = "oa_configurator"
    test_db_pg: Annotated[str | None, RefTo(CDMDatabaseConfig, is_test=True)] = None
    test_db_sqlite: Annotated[str | None, RefTo(CDMDatabaseConfig, is_test=True)] = None
