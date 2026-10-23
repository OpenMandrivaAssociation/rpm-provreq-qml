Name:		rpm-provreq-qml
Version:	6.2.0
Release:	3
Summary:	RPM Provides/Requires generator for QML files
Group:		Development/KDE and Qt
BuildArch:	noarch
License:	GPL-3.0-or-later
URL:		https://github.com/OpenMandrivaAssociation/rpm-provreq-qml
Requires:	python
Requires:	lib64zstd
BuildRequires:	python
BuildRequires:	lib64zstd
Source0:	qml.attr
Source1:	qmldeps
Source2:	qmlelf.attr
Source3:	test_qmldeps.py

%description
RPM Provides and Requires generator for QML files.

Scans loose .qml and qmldir files, standalone .rcc resources, and QML
embedded in ELF binaries by Qt's resource compiler. C++ modules registered
with qmlRegisterType or qmltyperegistrar are provided too. Import
requirements are unversioned qml(Module) names. RPM_QML_REQUIRES=0 skips
requirements.

%prep

%build

%check
python %{S:3}

%install
mkdir -p %{buildroot}%{_rpmconfigdir}/fileattrs
install -c -m 0644 %{S:0} %{buildroot}%{_rpmconfigdir}/fileattrs/qml.attr
install -c -m 0644 %{S:2} %{buildroot}%{_rpmconfigdir}/fileattrs/qmlelf.attr
install -c -m 0755 %{S:1} %{buildroot}%{_rpmconfigdir}/qmldeps

%files
%{_rpmconfigdir}/fileattrs/qml.attr
%{_rpmconfigdir}/fileattrs/qmlelf.attr
%{_rpmconfigdir}/qmldeps
