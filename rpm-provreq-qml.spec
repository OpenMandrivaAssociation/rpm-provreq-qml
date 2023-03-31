Name:		rpm-provreq-qml
Version:	6.2.0
Release:	2
Summary:	RPM Provides/Requires generator for QML files
Group:		Development/KDE and Qt
BuildArch:	noarch
License:	GPLv3
Source0:	qml.attr
Source1:	qml.req
Source2:	qml.prov

%description
RPM Provides/Requires generator for QML files

This generator is used for both Qt 5.x and Qt 6.x.

%prep

%build

%install
mkdir -p %{buildroot}%{_rpmconfigdir}/fileattrs
install -c -m 0644 %{S:0} %{buildroot}%{_rpmconfigdir}/fileattrs/
install -c -m 0755 %{S:1} %{buildroot}%{_rpmconfigdir}/
install -c -m 0755 %{S:2} %{buildroot}%{_rpmconfigdir}/

%files
%{_rpmconfigdir}/fileattrs/qml.attr
%{_rpmconfigdir}/qml.req
%{_rpmconfigdir}/qml.prov
