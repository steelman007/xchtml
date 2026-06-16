class Xchtml < Formula
  include Language::Python::Virtualenv

  desc "Generate beautiful HTML reports from Xcode .xcresult bundles"
  homepage "https://github.com/igram7/xchtml"
  url "https://github.com/igram7/xchtml/archive/refs/tags/v1.0.0.tar.gz"
  sha256 "290d8df35e2e42f355b3dcdccd97419ac6d5e50284402f16fbb2aeddc5de9ed2"
  license "MIT"

  depends_on "python@3.12"
  depends_on :macos

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "xchtml", shell_output("#{bin}/xchtml --version")
  end
end
