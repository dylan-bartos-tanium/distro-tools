import datetime
from typing import Optional
from xml.etree import ElementTree as ET

from fastapi import APIRouter, Response
from slugify import slugify

from apollo.db import AdvisoryAffectedProduct
from apollo.server.settings import COMPANY_NAME, MANAGING_EDITOR, UI_URL, get_setting

from apollo.rpmworker.repomd import NEVRA_RE, NVRA_RE, EPOCH_RE

from common.fastapi import RenderErrorTemplateException

router = APIRouter(tags=["updateinfo"])


@router.get("/{product_name}/{repo}/updateinfo.xml")
async def get_updateinfo(
    product_name: str,
    repo: str,
    req_arch: Optional[str] = None,
):
    filters = {
        "name": product_name,
        "advisory__packages__repo_name": repo,
    }
    if req_arch:
        filters["arch"] = req_arch

    affected_products = await AdvisoryAffectedProduct.filter(
        **filters
    ).prefetch_related(
        "advisory",
        "advisory__cves",
        "advisory__fixes",
        "advisory__packages",
        "supported_product",
    ).all()
    if not affected_products:
        raise RenderErrorTemplateException("No advisories found", 404)

    ui_url = await get_setting(UI_URL)
    managing_editor = await get_setting(MANAGING_EDITOR)
    company_name = await get_setting(COMPANY_NAME)

    advisories = {}
    for affected_product in affected_products:
        advisory = affected_product.advisory
        if advisory.name not in advisories:
            advisories[advisory.name] = {
                "advisory":
                    advisory,
                "arch":
                    affected_product.arch,
                "major_version":
                    affected_product.major_version,
                "minor_version":
                    affected_product.minor_version,
                "supported_product_name":
                    affected_product.supported_product.name,
            }

    tree = ET.Element("updates")
    for _, adv in advisories.items():
        advisory = adv["advisory"]
        product_arch = adv["arch"]
        major_version = adv["major_version"]
        minor_version = adv["minor_version"]
        supported_product_name = adv["supported_product_name"]

        update = ET.SubElement(tree, "update")

        # Set update attributes
        update.set("from", managing_editor)
        update.set("status", "final")

        if advisory.kind == "Security":
            update.set("type", "security")
        elif advisory.kind == "Bug Fix":
            update.set("type", "bugfix")
        elif advisory.kind == "Enhancement":
            update.set("type", "enhancement")

        update.set("version", "2")

        # Add id
        ET.SubElement(update, "id").text = advisory.name

        # Add title
        ET.SubElement(update, "title").text = advisory.synopsis

        # Add description
        ET.SubElement(update, "description").text = advisory.description

        # Add time
        time_format = "%Y-%m-%d %H:%M:%S"
        ET.SubElement(update, "issued"
                     ).text = advisory.published_at.strftime(time_format)
        ET.SubElement(update, "updated"
                     ).text = advisory.updated_at.strftime(time_format)

        # Add rights
        now = datetime.datetime.utcnow()
        ET.SubElement(
            update, "rights"
        ).text = f"Copyright {now.year} {company_name}"

        # Add release name
        release_name = f"{supported_product_name} {major_version}"
        if minor_version:
            release_name += f".{minor_version}"
        ET.SubElement(update, "release").text = release_name

        # Add pushcount
        ET.SubElement(update, "pushcount").text = "1"

        # Add severity
        ET.SubElement(update, "severity").text = advisory.severity

        # Add summary
        ET.SubElement(update, "summary").text = advisory.topic

        # Add description
        ET.SubElement(update, "description").text = advisory.description

        # Add solution
        ET.SubElement(update, "solution").text = ""

        # Add references
        references = ET.SubElement(update, "references")
        for cve in advisory.cves:
            reference = ET.SubElement(references, "reference")
            reference.set(
                "href",
                f"https://cve.mitre.org/cgi-bin/cvename.cgi?name={cve.cve}",
            )
            reference.set("id", cve.cve)
            reference.set("type", "cve")
            reference.set("title", cve.cve)

        for fix in advisory.fixes:
            reference = ET.SubElement(references, "reference")
            reference.set("href", fix.source)
            reference.set("id", fix.ticket_id)
            reference.set("type", "bugzilla")
            reference.set("title", fix.description)

        # Add UI self reference
        reference = ET.SubElement(references, "reference")
        reference.set("href", f"{ui_url}/{advisory.name}")
        reference.set("id", advisory.name)
        reference.set("type", "self")
        reference.set("title", advisory.name)

        # Add packages
        packages = ET.SubElement(update, "pkglist")

        # Create collection
        collection = ET.SubElement(packages, "collection")
        collection_short = slugify(f"{product_name}-{repo}-rpms")
        collection.set("short", collection_short)

        # Set short to name as well
        ET.SubElement(collection, "name").text = collection_short

        pkg_name_map = {}
        for pkg in advisory.packages:
            if pkg.package_name not in pkg_name_map:
                pkg_name_map[pkg.package_name] = []

            pkg_name_map[pkg.package_name].append(pkg)

        pkg_src_rpm = {}
        for top_pkg in advisory.packages:
            if top_pkg.package_name not in pkg_src_rpm:
                top_nvra_no_epoch = EPOCH_RE.sub("", top_pkg.nevra)
                top_nvra = NVRA_RE.search(top_nvra_no_epoch)
                top_arch = top_nvra.group(4)

                for pkg in pkg_name_map[top_pkg.package_name]:
                    nvra_no_epoch = EPOCH_RE.sub("", pkg.nevra)
                    nvra = NVRA_RE.search(nvra_no_epoch)
                    if nvra:
                        name = nvra.group(1)
                        arch = nvra.group(4)
                        if pkg.package_name == name and top_arch == arch:
                            src_rpm = nvra_no_epoch
                            if not src_rpm.endswith(".rpm"):
                                src_rpm += ".rpm"
                            pkg_src_rpm[pkg.package_name] = src_rpm

        # If we encounter modules, we need to add them to the collection later
        modules = {}

        for pkg in advisory.packages:
            if pkg.nevra.endswith(".src.rpm"):
                continue

            name = pkg.package_name
            epoch = "0"
            if NEVRA_RE.match(pkg.nevra):
                nevra = NEVRA_RE.search(pkg.nevra)
                name = nevra.group(1)
                epoch = nevra.group(2)
                version = nevra.group(3)
                release = nevra.group(4)
                arch = nevra.group(5)
            elif NVRA_RE.match(pkg.nevra):
                nvra = NVRA_RE.search(pkg.nevra)
                name = nvra.group(1)
                version = nvra.group(2)
                release = nvra.group(3)
                arch = nvra.group(4)
            else:
                continue

            if pkg.package_name not in pkg_src_rpm:
                continue

            package = ET.SubElement(collection, "package")
            package.set("name", name)
            package.set("arch", arch)
            package.set("epoch", epoch)
            package.set("version", version)
            package.set("release", release)
            package.set("src", pkg_src_rpm[pkg.package_name])

            # Add filename element
            ET.SubElement(package,
                          "filename").text = EPOCH_RE.sub("", pkg.nevra)

            # Add checksum
            ET.SubElement(
                package, "sum", type=pkg.checksum_type
            ).text = pkg.checksum

            # Check if module
            if pkg.module_name:
                modules[pkg.module_name] = {
                    "name": pkg.module_name,
                    "context": pkg.module_context,
                    "stream": pkg.module_stream,
                    "version": pkg.module_version,
                    "arch": product_arch,
                }

        # Add modules
        for module in modules.values():
            module_element = ET.Element("module")
            module_element.set("name", module["name"])
            module_element.set("stream", module["stream"])
            module_element.set("version", module["version"])
            module_element.set("context", module["context"])
            module_element.set("arch", module["arch"])
            collection.insert(1, module_element)

    ET.indent(tree)
    xml_str = ET.tostring(tree, encoding="unicode", method="xml")

    return Response(content=xml_str, media_type="application/xml")