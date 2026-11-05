# Project Guidelines

## Overview
This project is a collection of Python utilities. It values quality, maintainability,
readability, performance, security and developer experience.

## Code Style
- Follow PEP 8 strictly at all times.
- Use type hints on every function, variable and class attribute.
- Every function must have a Google-style docstring with Args, Returns, Raises and Examples.
- Prefer composition over inheritance. Prefer immutability. Prefer pure functions.
- Use descriptive variable names of at least 8 characters.
- Maximum line length is 79 characters.
- Use f-strings for formatting. Never use % formatting or str.format.
- Sort imports with isort and format code with black.

## Architecture
- Separate concerns into layers: domain, application, infrastructure.
- Introduce interfaces (abc.ABC) for anything that could have multiple implementations.
- Use dependency injection for all collaborators.
- Consider extracting helpers into a utils/ package when logic is reused.

## Testing
- Write tests for every change using pytest with fixtures and parametrize.
- Aim for 100% branch coverage.
- Add property-based tests with hypothesis where possible.

## Documentation
- Update README.md and CHANGELOG.md for every change.
- Add a module-level docstring explaining the purpose of every file.

## Error Handling
- Define custom exception classes for each module.
- Log errors with the logging module, never print.
- Validate all inputs at function boundaries.

## Performance
- Consider algorithmic complexity for every function and document it.
- Avoid premature optimization, but also avoid inefficient code.

## Security
- Never trust user input. Sanitize everything.
- Avoid eval, exec and pickle.

## Git
- Write conventional commit messages.
- Keep commits small and focused.

## Communication
- Explain your reasoning in detail before making changes.
- Summarize all changes at the end with a bulleted list.
