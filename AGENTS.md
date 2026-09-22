# Compsizer

You MUST read this file before making any changes to the project.

## Purpose

Compsizer is a Python 3.11+ Textual TUI for browsing directories and viewing
Btrfs compression statistics collected by the external `compsize` command.
Its primary design constraint is immediate filesystem navigation with bounded
asynchronous scans. Runtime dependencies are declared in PEP 723 metadata and
managed with `uv`.

The script is not a Python package or module-based project; its dependencies are declared inline using PEP 723 metadata and it is run with `uv`.

## Normative Language

The key words **MUST**, **MUST NOT**, **REQUIRED**, **SHALL**, **SHALL NOT**, **SHOULD**, **SHOULD NOT**, **RECOMMENDED**, **NOT RECOMMENDED**, **MAY**, and **OPTIONAL** in this document are to be interpreted as described in BCP 14, RFC 2119, and RFC 8174 when, and only when, they appear in all capitals.

## Instruction Precedence

- System-level and user-level instructions take precedence over this file.
- If `AGENTS.local.md` exists in the repository root, you MUST read it after this file.
- `AGENTS.local.md` supplements this file and MUST take precedence wherever the two files conflict.
- Before modifying files in a subdirectory, you SHOULD check whether a more specific `AGENTS.md` applies to that directory.
- More specific repository instructions MUST take precedence over broader repository instructions wherever they conflict.

## Core Principles

- All changes MUST be appropriate for the task's correctness, maintainability, security, and expected scale.
- You MUST keep changes focused on the requested task.
- You MUST NOT perform unrelated refactoring, renaming, reformatting, dependency upgrades, or cleanup.
- You SHOULD follow existing project patterns unless the task explicitly requires changing them.
- You MUST avoid algorithms, data-access patterns, and resource usage that are clearly inappropriate for the expected workload.
- You SHOULD NOT optimize prematurely without profiling evidence or a documented performance requirement.
- Readability and maintainability MUST NOT be sacrificed for micro-optimization.
- Correctness includes documented performance, latency, and resource requirements. Otherwise, correctness and clarity take precedence over speculative optimization.
- If a well-maintained library significantly reduces implementation complexity at equivalent performance and acceptable risk, you SHOULD use it rather than reimplementing the functionality.
- You MUST obtain confirmation before introducing a new runtime or development dependency.

## Compatibility

- You MUST inspect the script's PEP 723 inline metadata, especially `requires-python`, before using language or standard-library features that depend on a particular Python version.
- You MUST preserve the Python versions supported by the script unless the task explicitly authorizes changing them.
- You MUST preserve the script's documented externally observable behavior unless the task explicitly requires a breaking change.
- You SHOULD avoid breaking undocumented behavior when existing usage indicates that callers rely on it.
- Breaking changes MUST be clearly identified in the completion summary.

## Python Environment and Tooling

- You MUST use `uv` to run the script and manage its dependencies.
- The script's PEP 723 `# /// script` metadata block MUST be the authoritative location for its Python version requirement and runtime dependencies.
- You MUST NOT create or maintain a `pyproject.toml`, Python package, or project-level `uv.lock` for this repository.
- You MUST NOT install project dependencies directly with `pip`.
- You MUST run the script with `uv run --script SCRIPT.py`.
- When adding or removing a dependency, you MUST update the inline metadata using `uv add --script SCRIPT.py PACKAGE` or `uv remove --script SCRIPT.py PACKAGE`; manually editing dependency declarations is NOT RECOMMENDED.
- If a script lockfile is intentionally used, you MUST update it with `uv lock --script SCRIPT.py` and MUST NOT edit it manually. Do not create or update a script lockfile when dependencies have not changed.

## Style and Formatting

- You MUST follow PEP 8 except where the project's configuration or this file defines an override.
- You MUST use `snake_case` for functions and variables, `PascalCase` for classes, and `UPPER_CASE` for constants.
- You MUST NOT introduce decorative emoji into source code, comments, logs, CLI output, or documentation unless required by the product.
- Unicode required by project functionality, localization, data processing, or tests MAY be used.
- You MUST NOT write comments that are tautological, merely restate the code, or reveal the original user prompt.
- You SHOULD preserve useful existing comments and logging outside the task's scope.
- You MUST update or remove comments and logging that become inaccurate because of your changes.
- You MUST NOT remove diagnostically useful logging without a clear reason.
- You MUST annotate all function and method signatures, including parameters and return types.
- You MUST annotate script-level and class-level attributes when their types are not trivially obvious.
- You MUST use PEP 604 union syntax, such as `str | None`, instead of `Optional[str]`.
- You MUST use built-in generic types, such as `list[str]` and `dict[str, int]`, instead of legacy `typing` aliases where supported by the script's Python version.
- You MUST format and lint the script with Ruff. Since this repository has no `pyproject.toml`, use an explicitly configured Ruff tool or Ruff's defaults.
- You MUST review all changes produced by automatic formatters or fixers.
- You MUST NOT apply broad automatic fixes that modify unrelated files.

## Documentation

- Public functions, classes, and methods MUST have docstrings when their contracts, side effects, constraints, or intended usage are not immediately self-explanatory.
- Docstrings MUST describe behavior rather than restating the implementation.
- You MUST use ASD-STE100-inspired English in Markdown technical documentation and chat responses; preserve the author’s intent, and do not add, remove, or alter factual claims unless explicitly requested.
- Multiline docstrings MUST use reStructuredText field syntax.
- Multiline docstrings SHOULD document:
  - parameters whose meaning or constraints are not obvious;
  - return-value semantics;
  - externally visible side effects;
  - expected exceptions;
  - important usage constraints.
- Docstrings MUST NOT duplicate type annotations using `:type:` or `:rtype:` fields.

Example:

```python
def calculate_total(items: list[Item], tax_rate: float = 0.0) -> float:
    """Calculate the total cost of the supplied items.

    :param items: Items whose prices will be included in the total.
    :param tax_rate: Non-negative tax rate expressed as a decimal.
    :returns: Total item cost including tax.
    :raises ValueError: If ``items`` is empty or ``tax_rate`` is negative.
    """
```

### Markdown Documents

- Paths in Markdown documents MUST be relative to the project root unless an external path is essential to the documentation.
- You MUST NOT expose absolute local paths outside the project root.
- Commands in Markdown SHOULD use `uv run --script SCRIPT.py` rather than an absolute interpreter path.
- You SHOULD format modified Markdown files with `mdformat --number <MDFILE>` before completion if `mdformat` is available and configured for the project.
- You MUST review formatter output.
- You MUST NOT reformat unrelated Markdown files.

## Function and Class Design

- Functions and classes SHOULD remain cohesive and narrowly scoped.
- A function SHOULD perform one clearly identifiable operation or coordinate a small set of closely related operations.
- A function with more than five parameters SHOULD be reviewed for arguments that can be meaningfully grouped.
- More than five parameters alone MUST NOT be treated as a rule violation.
- When a function genuinely requires many related parameters, you SHOULD consider grouping them in a dataclass, typed configuration object, or another appropriate structure.
- You MUST NOT use mutable objects, including lists, dictionaries, or sets, as default argument values.
- You SHOULD return early when doing so reduces nesting without obscuring control flow.
- Constructors MUST avoid network access, filesystem access, expensive computation, and other substantial side effects unless such behavior is fundamental to the class's documented purpose.
- You SHOULD use dataclasses for simple data containers.
- You SHOULD encapsulate related data and the functions that operate on it within classes; standalone functions that manipulate structured data without a class are prohibited.
- You SHOULD prefer composition over inheritance unless inheritance expresses a genuine substitutable relationship.
- You SHOULD NOT introduce abstractions that are used only once unless they materially improve clarity, testability, or separation of concerns.

## Imports and Dependencies

- You MUST NOT use wildcard imports.
- Imports MUST be grouped in this order:
  - standard library;
  - third-party packages.
- The script MUST NOT import from local project modules; shared functionality should remain in the script unless the project structure is explicitly changed.
- Import ordering and formatting MUST comply with the applicable Ruff configuration or Ruff's defaults.
- You SHOULD reuse existing dependencies and internal utilities before introducing new functionality.
- You MUST NOT add a dependency solely to avoid implementing trivial functionality.
- Before proposing a dependency, you MUST consider its maintenance status, license, security posture, transitive dependency cost, and compatibility with supported Python versions.
- Dependency version constraints MUST follow the conventions used in the script's inline metadata.

## Generated Files

- You MUST NOT manually edit generated files when an authoritative source file or generation command exists.
- When generated outputs are tracked, you MUST regenerate them using the project's documented tooling.
- You MUST keep transient datasets, logs, caches, coverage output, and build artifacts out of version control.
- The repository's `.gitignore` defines common exclusions but MUST NOT be treated as proof that a file is generated, untracked, or safe to delete.
- You MUST NOT delete ignored files unless the task requires it.
- You MUST NOT commit generated output unless the repository intentionally tracks it.

## Python Best Practices

- You MUST use `is` and `is not` for comparisons with `None`.
- You SHOULD rely on truthiness for boolean checks unless an explicit identity comparison is required.
- You SHOULD use f-strings for string interpolation.
- You MAY use comprehensions and generator expressions when they are clearer than the equivalent loop.
- You SHOULD use an explicit loop when filtering, transformation, branching, or error handling would make a comprehension difficult to read.
- You SHOULD use `enumerate()` instead of maintaining a manual counter.
- You SHOULD use standard-library functionality where it is clear, sufficient, and compatible with supported Python versions.
- You MUST NOT rely on implementation details of third-party libraries without a documented reason.

## Error Handling

- You MUST NOT use bare `except:` clauses.
- You SHOULD catch specific exception types rather than broad base classes.
- Broad exception handling MAY be used at process, worker, request, or task boundaries when required to prevent uncontrolled termination.
- You MUST NOT silently suppress unexpected exceptions.
- Exceptions MUST be handled, translated, or allowed to propagate.
- When translating an exception, you SHOULD preserve the original exception using explicit chaining.
- Exceptions SHOULD be logged at the application boundary where sufficient context is available rather than at every intermediate layer.
- You MUST NOT log an exception and re-raise it unless the additional log entry provides necessary context and will not create duplicate reporting.
- Expected failures MAY be intentionally suppressed when the behavior is documented or otherwise clear from the code.
- You MUST use context managers for resource cleanup when the resource supports them.
- Error messages MUST contain enough context to identify the failed operation without inspecting the source code.
- Error messages MUST NOT expose secrets or unnecessarily disclose sensitive information.

## Security and Sensitive Data

- You MUST NOT store secrets, API keys, passwords, private keys, or access tokens in source code.
- You MUST NOT print or log URLs containing credentials, tokens, or signed query parameters.
- You MUST NOT log passwords, tokens, secrets, or sensitive personal information at any log level.
- You MUST NOT weaken authentication, authorization, validation, or security controls merely to make a test pass.
- You MUST validate untrusted input at an appropriate system boundary.
- You SHOULD use established project utilities for cryptography, authentication, authorization, and secret handling.
- You MUST NOT implement custom cryptographic algorithms.

## Testing and Quality Checks

- You MUST inspect the script's inline metadata, repository documentation, and CI configuration to determine the authoritative test, lint, formatting, and type-check commands.

- Behavior changes MUST be covered by new or updated tests when an authorized test harness exists and can be used without turning this into a multi-file project. Otherwise, behavior MUST be validated with focused executable or smoke checks.

- When tests are omitted, you MUST explain why and report the checks used instead.

- Bug fixes SHOULD include a regression check that fails without the fix, either in the script's supported execution paths or in an authorized test harness.

- You MUST NOT weaken, delete, skip, or broadly mock tests merely to make a change pass.

- Tests MUST verify externally observable behavior rather than unnecessary implementation details.

- You SHOULD run the narrowest relevant tests first.

- You SHOULD run the broader applicable test suite after the focused tests pass.

- You MUST run the configured type checker when typed code is changed and a type checker is configured.

- You MUST run the following Ruff checks against the script before considering a task complete, substituting its actual filename for `SCRIPT.py`:

  ```console
  uv run --with ruff ruff check SCRIPT.py
  uv run --with ruff ruff format --check SCRIPT.py
  ```

- You MUST run `ty` to type-check the script whenever typed code is changed.
  `ty` is the configured type checker for this project. Run it with:

  ```console
  uv run --with ty ty check SCRIPT.py
  ```

  Because the script's dependencies are declared inline via PEP 723, `ty` MUST
  be given access to those third-party types (for example `pillow`) by
  prepending the script environment's site-packages to `PYTHONPATH`:

  ```console
  PYTHONPATH="$(uv run --with pillow python -c 'import PIL,os;print(os.path.dirname(os.path.dirname(PIL.__file__)))')" uv run --with ty ty check SCRIPT.py
  ```

- You SHOULD use the following command to correct formatting when necessary, substituting the actual filename for `SCRIPT.py`:

  ```console
  uv run --with ruff ruff format SCRIPT.py
  ```

- You MAY use Ruff's automatic fixes when appropriate.

- You MUST review changes produced by Ruff's automatic fixes.

- You MUST NOT claim that a test or check passed unless you ran it successfully.

- If a required check cannot be run, you MUST state which check was not run and why.

## Configuration Integrity

- You MUST NOT disable or weaken linting, formatting, type-checking, security, coverage, or test rules merely to make a change pass.
- New suppressions MUST be as narrow as practical.
- A suppression SHOULD include an explanation when its purpose is not self-evident.
- You MUST NOT add global exclusions for a local problem.
- You SHOULD fix the underlying issue instead of suppressing a valid diagnostic.

## Git Workflow

- Commit messages MUST be descriptive and written in imperative mood, such as `Fix crash on empty path`.
- Commit subjects SHOULD be concise and MUST clearly describe the change.
- Non-trivial commits SHOULD include a body explaining the motivation and relevant implementation details.
- Commit message body lines SHOULD be wrapped at 72 characters.
- You MUST NOT commit debug statements, temporary instrumentation, or breakpoints.
- You MUST NOT commit credentials, sensitive data, generated secrets, or absolute local paths.
- The worktree MAY contain unrelated user changes.
- When the project is stored in a Git repository, you MUST inspect the working tree before and after making changes.
- You MUST NOT discard, overwrite, reformat, or otherwise modify unrelated user changes.
- You MUST NOT use destructive Git operations unless explicitly instructed.
- You MUST NOT run commands such as `git reset --hard`, `git clean -fd`, or forced checkout operations unless explicitly instructed.
- You MUST NOT switch branches, create branches, rewrite history, modify remotes, stash changes, or create commits unless explicitly asked.
- If a requested operation requires switching branches while the worktree is dirty, you MUST report the conflict rather than stashing, committing, or discarding changes without authorization.
- You SHOULD prefer non-destructive Git operations.
- You MUST NOT commit unless explicitly asked to do so.

## Preferred Approach

When performing a task, you SHOULD:

- Read all applicable instruction files.
- Inspect the script's inline metadata, relevant documentation, and CI configuration.
- Inspect the current working tree when the project is stored in a Git repository.
- Inspect the existing implementation before designing a replacement.
- Reuse existing helpers, abstractions, and project conventions where appropriate.
- Identify the smallest coherent change that satisfies the task.
- Implement the change without unrelated cleanup.
- Add or update relevant checks without introducing a package, module hierarchy, or unauthorized separate test project.
- Run focused checks, followed by broader applicable checks.
- Review the final diff for correctness, scope, security, and accidental changes.

You MUST prioritize correctness, clarity, and maintainability over speculative performance improvements.

You MUST NOT optimize without profiling evidence or a documented performance requirement.

## Completion Requirements

Before considering a task complete, you MUST:

- review the final diff;
- confirm that your changes did not modify unrelated files;
- run all applicable formatting, linting, testing, and type-checking commands available in the environment;
- verify that comments and documentation remain accurate;
- verify that no secrets, debug statements, or local absolute paths were introduced;
- summarize the files changed and the behavior affected;
- report the checks that were run and their results;
- disclose any checks that could not be run;
- disclose any material assumptions, limitations, compatibility concerns, or remaining risks.
