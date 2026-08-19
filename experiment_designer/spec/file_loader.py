"""
File Loader - Load external data files (CSV, JSON, YAML) for experiment specifications.

Supports:
- CSV, JSON, YAML file formats (detected by extension)
- Selection strategies: all, random, first, filter
- Column mapping for CSV files
- Path resolution relative to spec file location
"""

import csv
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class FileReference:
    """
    Represents a reference to an external data file.

    Attributes:
        path: File path (relative to spec file or absolute)
        format: File format (csv, json, yaml) - auto-detected if not specified
        select: Selection strategy (all, random, first, filter)
        count: Number of records to select (for random, first)
        filter: Filter criteria as dict of field -> value
        columns: Column mapping for CSV (source_name -> target_name)
    """

    path: str
    format: str | None = None
    select: str = "all"
    count: int | None = None
    filter: dict[str, Any] | None = None
    columns: dict[str, str] | None = None

    def __post_init__(self):
        """Auto-detect format from file extension if not specified."""
        if self.format is None:
            ext = Path(self.path).suffix.lower()
            format_map = {
                ".csv": "csv",
                ".json": "json",
                ".yaml": "yaml",
                ".yml": "yaml",
            }
            self.format = format_map.get(ext, "csv")


class FileLoader:
    """
    Load and process external data files.

    Example usage:
        loader = FileLoader(base_dir=Path("/path/to/spec"))

        # Simple file reference
        ref = FileReference(path="./data/outlets.csv")
        data = loader.load(ref)

        # With options
        ref = FileReference(
            path="./data/outlets.csv",
            select="random",
            count=20,
            filter={"region": "US"},
            columns={"outlet_name": "domain", "website": "url"}
        )
        data = loader.load(ref)
    """

    def __init__(self, base_dir: Path | None = None, seed: int | None = None):
        """
        Initialize file loader.

        Args:
            base_dir: Base directory for resolving relative paths
            seed: Random seed for reproducible selections
        """
        self.base_dir = base_dir or Path.cwd()
        self.rng = random.Random(seed)
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self._cache: dict[str, list[dict[str, Any]]] = {}

    def load(
        self, ref: FileReference, context: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """
        Load and process a file reference.

        Args:
            ref: FileReference configuration
            context: Optional context for variable substitution in filter values

        Returns:
            List of dictionaries representing the loaded records
        """
        # Resolve path
        path = self._resolve_path(ref.path)

        if not path.exists():
            self.errors.append(f"File not found: {path}")
            return []

        # Check cache
        cache_key = str(path)
        if cache_key not in self._cache:
            # Load raw data
            raw_data = self._load_file(path, ref.format)
            if raw_data is None:
                return []
            self._cache[cache_key] = raw_data

        data = self._cache[cache_key].copy()

        # Apply column mapping
        if ref.columns:
            data = self._apply_column_mapping(data, ref.columns)

        # Apply filter
        if ref.filter:
            data = self._apply_filter(data, ref.filter, context)

        # Apply selection strategy
        data = self._apply_selection(data, ref.select, ref.count)

        return data

    def _resolve_path(self, path: str) -> Path:
        """Resolve file path relative to base directory."""
        p = Path(path)
        if p.is_absolute():
            return p
        return self.base_dir / p

    def _load_file(self, path: Path, format: str) -> list[dict[str, Any]] | None:
        """
        Load a file and return list of records.

        Args:
            path: Resolved file path
            format: File format (csv, json, yaml)

        Returns:
            List of dictionaries or None on error
        """
        try:
            if format == "csv":
                return self._load_csv(path)
            elif format == "json":
                return self._load_json(path)
            elif format == "yaml":
                return self._load_yaml(path)
            else:
                self.errors.append(f"Unsupported file format: {format}")
                return None
        except Exception as e:
            self.errors.append(f"Error loading {path}: {e}")
            return None

    def _load_csv(self, path: Path) -> list[dict[str, Any]]:
        """Load CSV file using DictReader."""
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            data = []
            for row in reader:
                # Convert numeric strings to numbers
                processed = {}
                for key, value in row.items():
                    if key is None:
                        continue
                    processed[key] = self._convert_value(value)
                data.append(processed)
            return data

    def _load_json(self, path: Path) -> list[dict[str, Any]]:
        """Load JSON file."""
        with open(path, encoding="utf-8") as f:
            content = json.load(f)

        # Handle both array and object with 'data' key
        if isinstance(content, list):
            return content
        elif isinstance(content, dict):
            if "data" in content:
                return content["data"]
            # Single object - wrap in list
            return [content]
        else:
            self.errors.append(f"JSON file must contain array or object: {path}")
            return []

    def _load_yaml(self, path: Path) -> list[dict[str, Any]]:
        """Load YAML file."""
        with open(path, encoding="utf-8") as f:
            content = yaml.safe_load(f)

        # Handle both array and object with 'data' key
        if isinstance(content, list):
            return content
        elif isinstance(content, dict):
            if "data" in content:
                return content["data"]
            # Single object - wrap in list
            return [content]
        else:
            self.errors.append(f"YAML file must contain array or object: {path}")
            return []

    def _convert_value(self, value: str) -> Any:
        """Convert string value to appropriate type."""
        if value is None or value == "":
            return None

        # Try integer
        try:
            if "." not in value:
                return int(value)
        except ValueError:
            pass

        # Try float
        try:
            return float(value)
        except ValueError:
            pass

        # Try boolean
        if value.lower() in ("true", "yes", "1"):
            return True
        if value.lower() in ("false", "no", "0"):
            return False

        # Keep as string
        return value

    def _apply_column_mapping(
        self, data: list[dict[str, Any]], columns: dict[str, str]
    ) -> list[dict[str, Any]]:
        """
        Apply column mapping (rename columns).

        Args:
            data: List of records
            columns: Mapping from source column name to target name

        Returns:
            List of records with renamed columns
        """
        result = []
        for row in data:
            new_row = {}
            for key, value in row.items():
                # Use mapped name if available, otherwise keep original
                new_key = columns.get(key, key)
                new_row[new_key] = value
            result.append(new_row)
        return result

    def _apply_filter(
        self,
        data: list[dict[str, Any]],
        filter_spec: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Filter records by field values.

        Args:
            data: List of records
            filter_spec: Dict of field -> value to match
            context: Optional context for variable substitution

        Returns:
            Filtered list of records
        """
        result = []
        for row in data:
            matches = True
            for field, expected in filter_spec.items():
                actual = row.get(field)

                # Substitute variables in expected value if context provided
                if context and isinstance(expected, str):
                    for key, val in context.items():
                        expected = expected.replace(f"{{{key}}}", str(val))

                # Check match (case-insensitive for strings)
                if isinstance(actual, str) and isinstance(expected, str):
                    if actual.lower() != expected.lower():
                        matches = False
                        break
                elif actual != expected:
                    matches = False
                    break

            if matches:
                result.append(row)

        return result

    def _apply_selection(
        self, data: list[dict[str, Any]], strategy: str, count: int | None
    ) -> list[dict[str, Any]]:
        """
        Apply selection strategy.

        Args:
            data: List of records
            strategy: Selection strategy (all, random, first)
            count: Number of records to select

        Returns:
            Selected records
        """
        if strategy == "all" or count is None:
            return data

        if len(data) == 0:
            return data

        if count > len(data):
            self.warnings.append(f"Requested {count} records but only {len(data)} available")
            return data

        if strategy == "random":
            return self.rng.sample(data, count)
        elif strategy == "first":
            return data[:count]
        else:
            self.warnings.append(f"Unknown selection strategy: {strategy}")
            return data


def create_file_tag_loader(base_dir: Path, seed: int | None = None):
    """
    Create a custom YAML loader class with !file tag support.

    The !file tag can be used in two forms:

    1. Scalar form (simple path):
       search_terms: !file ./data/terms.csv

    2. Mapping form (with options):
       search_terms: !file
         path: ./data/terms.csv
         select: random
         count: 20
         filter:
           region: US
         columns:
           source_col: target_col

    Args:
        base_dir: Base directory for resolving relative paths
        seed: Random seed for reproducible selections

    Returns:
        Custom SafeLoader class with !file constructor
    """
    file_loader = FileLoader(base_dir=base_dir, seed=seed)

    class FileTagLoader(yaml.SafeLoader):
        """Custom YAML loader with !file tag support."""

        pass

    def file_constructor(loader: yaml.SafeLoader, node: yaml.Node) -> dict[str, Any]:
        """
        Construct a file reference marker from YAML node.

        Returns a marker dict that will be resolved later by the parser.
        The marker contains either:
        - {"__file_ref__": "path/to/file"} for scalar form
        - {"__file_ref__": {path: ..., select: ..., ...}} for mapping form
        """
        if isinstance(node, yaml.ScalarNode):
            # Simple form: !file path/to/file
            path = loader.construct_scalar(node)
            return {"__file_ref__": path}
        elif isinstance(node, yaml.MappingNode):
            # Complex form: !file with options
            mapping = loader.construct_mapping(node)
            return {"__file_ref__": mapping}
        else:
            raise yaml.YAMLError(f"!file tag requires scalar or mapping, got {type(node)}")

    FileTagLoader.add_constructor("!file", file_constructor)

    return FileTagLoader, file_loader


def resolve_file_references(
    obj: Any, file_loader: FileLoader, context: dict[str, Any] | None = None
) -> Any:
    """
    Recursively resolve file reference markers in a data structure.

    Args:
        obj: Data structure potentially containing __file_ref__ markers
        file_loader: FileLoader instance for loading files
        context: Optional context for variable substitution

    Returns:
        Data structure with file references replaced by loaded data
    """
    if isinstance(obj, dict):
        if "__file_ref__" in obj:
            # This is a file reference marker
            ref_data = obj["__file_ref__"]

            if isinstance(ref_data, str):
                # Simple form: just a path
                ref = FileReference(path=ref_data)
            elif isinstance(ref_data, dict):
                # Complex form: dict with options
                ref = FileReference(
                    path=ref_data.get("path", ""),
                    format=ref_data.get("format"),
                    select=ref_data.get("select", "all"),
                    count=ref_data.get("count"),
                    filter=ref_data.get("filter"),
                    columns=ref_data.get("columns"),
                )
            else:
                file_loader.errors.append(f"Invalid file reference: {ref_data}")
                return []

            return file_loader.load(ref, context)
        else:
            # Regular dict - recurse into values
            return {k: resolve_file_references(v, file_loader, context) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [resolve_file_references(item, file_loader, context) for item in obj]
    else:
        return obj
