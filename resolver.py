import fnmatch
import yaml
from copy import deepcopy
from pathlib import Path

class Resolver:
    def __init__(
        self,
        yaml_path: Path,
        fiid: str,
        log
    ):
        self.yaml_path = yaml_path
        self.fiid = fiid
        self.site = f"{fiid}999"
        self.log = log

    def load(self):
        with open(
            self.yaml_path,
            encoding="utf-8"
        ) as f:
            data = yaml.safe_load(f)

        # FIX: Manejo si YAML es lista directa o diccionario con clave 'routes'
        if isinstance(data, list):
            raw_routes = data
        else:
            raw_routes = data.get("routes", [])

        routes = []
        for original_route in raw_routes:
            # ... [resto de la lógica de filtrado sin cambios] ...
            route = deepcopy(original_route)

            # --- FILTRADO DE ENTIDAD INTEGRADO ---
            fiid_filter = route.get("fiid_filter")
            if fiid_filter:
                if isinstance(fiid_filter, list):
                    if self.fiid not in set(fiid_filter):
                        continue
                elif fiid_filter != self.fiid:
                    continue

            site_filter = route.get("site_filter")
            if site_filter and site_filter != self.site:
                continue

            transport = route.get("transport")
            if not transport:
                raise ValueError(
                    f"route={route.get('lnk')} missing_transport"
                )

            # Reemplaza $FIID para rutas SMB
            if transport["type"] == "smb" and "unc_path" in transport:
                transport["unc_path"] = (
                    transport["unc_path"]
                    .replace("$FIID", self.fiid)
                )

            # Logica original para SFTP
            if transport["type"] == "sftp" and "remote" in transport:
                transport["remote"] = (
                    transport["remote"]
                    .replace("$FIID", self.fiid)
                    .replace("$SITE", self.site)
                )

            routes.append(route)

        self.log.debug(f"resolver_loaded routes={len(routes)}")
        return routes

    def match(self, filename, routes):
        matched = []
        filename = filename.upper()
        for route in routes:
            for pattern in route.get("patterns", []):
                if fnmatch.fnmatchcase(
                    filename,
                    pattern.upper()
                ):
                    matched.append(route)
                    break
        return matched