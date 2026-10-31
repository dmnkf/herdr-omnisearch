# Herdr's program table, from herdr-app defaults/programs.json (0.1.10): what it looks past, what it knows,
# and the agents people start from it.
SHELLS = {
    "bash", "cmd", "csh", "dash", "elvish", "fish", "ion", "ksh", "nu", "powershell", "pwsh", "sh", "tcsh", "xonsh",
    "zsh",
}

INTERPRETERS = {
    "bun", "bundle", "bunx", "deno", "env", "nice", "node", "nodemon", "npx", "pnpm", "python", "python3", "ruby",
    "sudo", "time", "uv", "uvx", "watch", "yarn",
}

KNOWN_PROGRAMS = {
    "agent", "agy", "amp", "ansible", "ansible-playbook", "antigravity", "apt", "apt-get", "artisan", "astro",
    "bat", "biome", "brew", "btm", "btop", "cabal", "caddy", "cargo", "claude", "claude-code", "cmake", "codex",
    "command-code", "composer", "copilot", "crush", "curl", "cursor", "cursor-agent", "dart", "django-admin", "dnf",
    "docker", "docker-compose", "dotnet", "dpkg", "dune", "electron", "elixir", "emacs", "erl", "esbuild", "eslint",
    "et", "expo", "fastapi", "fd", "ffmpeg", "find", "flask", "flutter", "fly", "flyctl", "fzf", "gcloud", "gemini",
    "gh", "ghc", "ghci", "git", "github-copilot", "gitui", "glab", "glances", "go", "gradle", "gradlew", "grep",
    "grok", "helix", "helm", "hermes", "htop", "http", "httpie", "hx", "iex", "java", "javac", "jest", "jshell",
    "julia", "jupyter", "just", "k9s", "kimi", "kind", "kiro", "kiro-cli", "kotlin", "kotlinc", "kubectl",
    "lazydocker", "lazygit", "less", "lua", "luajit", "make", "man", "mariadb", "mastracode", "minikube", "mix",
    "mongo", "mongosh", "more", "mosh", "muse", "mvn", "mvnw", "mysql", "nano", "netlify", "next", "nginx", "ninja",
    "nix", "nix-shell", "nixos-rebuild", "npm", "npx", "nuxi", "nuxt", "nvim", "ocaml", "ollama", "opam",
    "opencode", "pacman", "paru", "perl", "php", "pi", "podman", "poetry", "postgres", "prettier", "prime-agent",
    "prisma", "program", "psql", "pytest", "qoder", "qodercli", "r", "redis-cli", "redis-server", "rg",
    "rpm-ostree", "rscript", "ruff", "rustc", "rustup", "sbt", "scala", "screen", "shell", "sqlite", "sqlite3",
    "ssh", "stack", "supabase", "swift", "tauri", "terraform", "tig", "tmux", "tofu", "top", "ts-node", "tsc",
    "tsx", "turbo", "utop", "vercel", "vi", "view", "vim", "vite", "vitest", "webpack", "wget", "wrangler", "xh",
    "yay", "zellij", "zig",
}

AGENT_PROGRAMS = {
    "agy", "amp", "antigravity", "claude", "claude-code", "codex", "copilot", "crush", "cursor", "cursor-agent",
    "gemini", "github-copilot", "grok", "hermes", "kimi", "kiro", "kiro-cli", "opencode", "pi", "qoder", "qodercli",
}
