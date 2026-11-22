"""Agent Skills frontmatter and read-only instruction manifests."""
import yaml

from extension_packages import read_member


MAX_FRONTMATTER = 16 * 1024
MAX_SKILL = 512 * 1024


def parse(root, directory_name):
    text = read_member(root / "SKILL.md", root, MAX_SKILL).decode("utf-8-sig")
    lines = text.splitlines()
    try:
        if not lines or lines[0] != "---":
            raise ValueError
        end = lines.index("---", 1)
        header = "\n".join(lines[1:end])
        if len(header.encode()) > MAX_FRONTMATTER:
            raise ValueError
        if any(isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)) for token in yaml.scan(header)):
            raise ValueError
        metadata = yaml.safe_load(header)
    except (yaml.YAMLError, RecursionError, ValueError) as error:
        raise ValueError("Invalid SKILL.md YAML frontmatter") from error
    if not isinstance(metadata, dict) or any(not isinstance(key, str) for key in metadata):
        raise ValueError("SKILL.md frontmatter must contain named fields")
    name, description = metadata.get("name"), metadata.get("description")
    if not isinstance(name, str) or not 1 <= len(name) <= 64 or name != directory_name:
        raise ValueError("Skill name must match its directory")
    if (name.startswith("-") or name.endswith("-") or "--" in name
            or not all(c == "-" or (c.isalnum() and c == c.lower()) for c in name)):
        raise ValueError("Invalid skill name")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise ValueError("Skill needs a description up to 1024 characters")
    compatibility = metadata.get("compatibility", "")
    if not isinstance(compatibility, str) or len(compatibility) > 500:
        raise ValueError("Invalid skill compatibility")
    dependencies = metadata.get("allowed-tools", "")
    if not isinstance(dependencies, str):
        raise ValueError("allowed-tools must be a string")
    tags = metadata.get("metadata", {})
    if not isinstance(tags, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in tags.items()):
        raise ValueError("Skill metadata must map strings to strings")
    return {"name": name, "description": description, "compatibility": compatibility,
            "tools_requested": dependencies.split(), "scripts_present": (root / "scripts").is_dir(),
            "permissions_granted": []}, text
