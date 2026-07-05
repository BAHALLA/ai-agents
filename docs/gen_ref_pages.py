import pathlib

import mkdocs_gen_files

# Map of source files to their destination in the docs
ROOT_FILES = {
    "CHANGELOG.md": "CHANGELOG.md",
    "CONTRIBUTING.md": "CONTRIBUTING.md",
    "SECURITY.md": "SECURITY.md",
    "LICENSE": "LICENSE.md",
}

# Copy root files and fix links
for src, dst in ROOT_FILES.items():
    with open(src) as f:
        content = f.read()
        # Fix links like [Adding an Agent](docs/adding-an-agent.md) -> [Adding an Agent](adding-an-agent.md)
        content = content.replace("docs/", "")
        # Fix relative links to root files that are now in the same directory
        content = content.replace("(LICENSE)", "(LICENSE.md)")

        with mkdocs_gen_files.open(dst, "w") as fd:
            fd.write(content)

# Generate pages for agents
agents_dir = pathlib.Path("agents")
for agent_path in agents_dir.iterdir():
    if agent_path.is_dir():
        readme_path = agent_path / "README.md"
        if readme_path.exists():
            with open(readme_path) as f:
                content = f.read()

                # Fix links like ../../docs/adr/002-agent-tool-vs-sub-agents.md -> ../adr/002-agent-tool-vs-sub-agents.md
                content = content.replace("../../docs/", "../")
                # Fix links like ../../README.md#configuration -> ../config/general.md
                content = content.replace("../../README.md#configuration", "../config/general.md")
                # Fix links like ../../README.md#slack-bot -> ../integrations/slack.md
                content = content.replace("../../README.md#slack-bot", "../integrations/slack.md")
                # Fix links like ../../README.md -> ../index.md
                content = content.replace("../../README.md", "../index.md")
                # Fix links to other agents like ../kafka-health/ -> kafka-health.md
                for other_agent in agents_dir.iterdir():
                    if other_agent.is_dir():
                        content = content.replace(
                            f"../{other_agent.name}/", f"{other_agent.name}.md"
                        )

                with mkdocs_gen_files.open(f"agents/{agent_path.name}.md", "w") as fd:
                    fd.write(content)

# Generate page for core
core_readme = pathlib.Path("core/README.md")
if core_readme.exists():
    with open(core_readme) as f:
        content = f.read()
        # Rewrite repo-relative doc/image links (e.g. ../docs/images/foo.png)
        # to site-relative ones (../images/foo.png). Images live under docs/images
        # and are served natively by mkdocs — no per-asset copy needed here.
        content = content.replace("../docs/", "../")

        with mkdocs_gen_files.open("core/README.md", "w") as fd:
            fd.write(content)
