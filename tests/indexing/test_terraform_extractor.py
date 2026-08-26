"""Tests для TerraformExtractor."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.indexing.graph.languages.terraform import TerraformExtractor


@pytest.fixture
def extractor() -> TerraformExtractor:
    return TerraformExtractor()


def _extract(extractor: TerraformExtractor, source: str, file_name: str = "main.tf"):
    return extractor.extract(Path(file_name), source=source.encode("utf-8"))


# ─── Variables / outputs / providers ────────────────────────────────


class TestVariables:
    def test_simple_variable(self, extractor: TerraformExtractor) -> None:
        res = _extract(extractor, '''
variable "region" {
  type    = string
  default = "us-east-1"
}
''')
        vars_ = [s for s in res.symbols if s.kind == "variable"]
        assert len(vars_) == 1
        assert vars_[0].name == "region"

    def test_multiple_variables(self, extractor: TerraformExtractor) -> None:
        res = _extract(extractor, '''
variable "region" {
  type = string
}

variable "instance_count" {
  type    = number
  default = 1
}

variable "tags" {
  type    = map(string)
  default = {}
}
''')
        vars_ = [s for s in res.symbols if s.kind == "variable"]
        names = {v.name for v in vars_}
        assert names == {"region", "instance_count", "tags"}


class TestOutputs:
    def test_output(self, extractor: TerraformExtractor) -> None:
        res = _extract(extractor, '''
output "instance_id" {
  value = aws_instance.web.id
}
''')
        outputs = [s for s in res.symbols if s.kind == "vendor.tf.output"]
        assert outputs[0].name == "instance_id"


class TestProviders:
    def test_provider(self, extractor: TerraformExtractor) -> None:
        res = _extract(extractor, '''
provider "aws" {
  region = "us-east-1"
}
''')
        providers = [s for s in res.symbols if s.kind == "vendor.tf.provider"]
        assert providers[0].name == "aws"


# ─── Resources ──────────────────────────────────────────────────────


class TestResources:
    def test_resource_qualified_name(self, extractor: TerraformExtractor) -> None:
        """`resource "aws_instance" "web"` → name='aws_instance.web', module='aws_instance'."""
        res = _extract(extractor, '''
resource "aws_instance" "web" {
  ami           = "ami-12345"
  instance_type = "t3.medium"
}
''')
        resources = [s for s in res.symbols if s.kind == "vendor.tf.resource"]
        assert len(resources) == 1
        assert resources[0].name == "aws_instance.web"
        assert resources[0].module == "aws_instance"

    def test_multiple_resources(self, extractor: TerraformExtractor) -> None:
        res = _extract(extractor, '''
resource "aws_instance" "web" { ami = "x" }
resource "aws_instance" "db"  { ami = "y" }
resource "aws_s3_bucket" "logs" { bucket = "z" }
''')
        resources = [s for s in res.symbols if s.kind == "vendor.tf.resource"]
        names = {r.name for r in resources}
        assert names == {"aws_instance.web", "aws_instance.db", "aws_s3_bucket.logs"}


class TestDataSources:
    def test_data_source(self, extractor: TerraformExtractor) -> None:
        res = _extract(extractor, '''
data "aws_ami" "ubuntu" {
  most_recent = true
}
''')
        data = [s for s in res.symbols if s.kind == "vendor.tf.data"]
        assert data[0].name == "aws_ami.ubuntu"
        assert data[0].module == "aws_ami"


# ─── Modules + IMPORTS ──────────────────────────────────────────────


class TestModuleImports:
    def test_module_with_registry_source(self, extractor: TerraformExtractor) -> None:
        """Public registry source → IMPORTS edge."""
        res = _extract(extractor, '''
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "5.1.0"
  cidr    = "10.0.0.0/16"
}
''')
        modules = [s for s in res.symbols if s.kind == "vendor.tf.module"]
        assert modules[0].name == "vpc"

        imports = [e for e in res.edges if e.kind == "IMPORTS"]
        assert imports[0].raw_target == "terraform-aws-modules/vpc/aws"

    def test_module_with_git_source(self, extractor: TerraformExtractor) -> None:
        res = _extract(extractor, '''
module "shared" {
  source = "git::https://github.com/myorg/terraform-modules.git//networking?ref=v1.0"
}
''')
        imports = [e for e in res.edges if e.kind == "IMPORTS"]
        assert "github.com/myorg" in imports[0].raw_target

    def test_module_local_source(self, extractor: TerraformExtractor) -> None:
        res = _extract(extractor, '''
module "local_mod" {
  source = "./modules/network"
}
''')
        imports = [e for e in res.edges if e.kind == "IMPORTS"]
        assert imports[0].raw_target == "./modules/network"

    def test_multi_module(self, extractor: TerraformExtractor) -> None:
        res = _extract(extractor, '''
module "vpc" { source = "terraform-aws-modules/vpc/aws" }
module "eks" { source = "terraform-aws-modules/eks/aws" }
''')
        modules = [s for s in res.symbols if s.kind == "vendor.tf.module"]
        names = {m.name for m in modules}
        assert names == {"vpc", "eks"}

        imports = {e.raw_target for e in res.edges if e.kind == "IMPORTS"}
        assert imports == {
            "terraform-aws-modules/vpc/aws",
            "terraform-aws-modules/eks/aws",
        }


# ─── Skipped block types ────────────────────────────────────────────


class TestSkippedBlocks:
    def test_terraform_block_no_symbols(self, extractor: TerraformExtractor) -> None:
        """`terraform { required_providers ... }` — config block, не symbol."""
        res = _extract(extractor, '''
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
''')
        # Тільки file_module symbol, no actual entities
        non_file = [s for s in res.symbols if s.kind != "file_module"]
        assert non_file == []

    def test_locals_block_skipped_for_mvp(self, extractor: TerraformExtractor) -> None:
        res = _extract(extractor, '''
locals {
  region   = "us-east-1"
  zones    = ["a", "b"]
}
''')
        non_file = [s for s in res.symbols if s.kind != "file_module"]
        assert non_file == []  # YAGNI per-local symbols


# ─── Real-world ─────────────────────────────────────────────────────


class TestRealWorld:
    def test_full_main_tf_smoke(self, extractor: TerraformExtractor) -> None:
        source = '''
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "env" {
  type = string
}

provider "aws" {
  region = var.region
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "5.1.0"
  cidr    = "10.0.0.0/16"
}

resource "aws_instance" "web" {
  ami           = "ami-12345"
  instance_type = "t3.medium"
  subnet_id     = module.vpc.public_subnets[0]
}

resource "aws_s3_bucket" "logs" {
  bucket = "${var.env}-logs"
}

data "aws_ami" "ubuntu" {
  most_recent = true
}

output "instance_id" {
  value = aws_instance.web.id
}
'''
        res = extractor.extract(Path("main.tf"), source=source.encode("utf-8"))
        assert not res.parse_errors

        kinds_count: dict[str, int] = {}
        for s in res.symbols:
            kinds_count[s.kind] = kinds_count.get(s.kind, 0) + 1

        assert kinds_count.get("variable") == 2
        assert kinds_count.get("vendor.tf.provider") == 1
        assert kinds_count.get("vendor.tf.module") == 1
        assert kinds_count.get("vendor.tf.resource") == 2
        assert kinds_count.get("vendor.tf.data") == 1
        assert kinds_count.get("vendor.tf.output") == 1

        # Module imports
        imports = [e for e in res.edges if e.kind == "IMPORTS"]
        assert len(imports) == 1
        assert imports[0].raw_target == "terraform-aws-modules/vpc/aws"
