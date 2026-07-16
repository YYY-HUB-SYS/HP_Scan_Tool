# WS-Discovery (WSD) Protocol Research for HP Scan Tool

> **Purpose**: Evaluate WS-Discovery as a parallel scanner discovery mechanism alongside the existing mDNS/zeroconf approach in `escl_engine.py`.
> **Date**: 2025
> **Status**: Research complete, ready for implementation planning

---

## Table of Contents

1. [WS-Discovery Protocol Basics](#1-ws-discovery-protocol-basics)
2. [HP Scanner WSD Registration](#2-hp-scanner-wsd-registration)
3. [Python Implementations](#3-python-implementations)
4. [WSD-to-eSCL Integration](#4-wsd-to-escl-integration)
5. [Practical Considerations](#5-practical-considerations)
6. [Implementation Recommendations](#6-implementation-recommendations)
7. [Sources](#7-sources)

---

## 1. WS-Discovery Protocol Basics

### 1.1 Overview

WS-Discovery (Web Services Discovery) is a SOAP-over-UDP multicast protocol that enables dynamic discovery of services on a local network. It was developed as part of the broader Web Services for Devices (WSD) / Devices Profile for Web Services (DPWS) ecosystem. The protocol is defined in the [WS-Discovery specification](https://specs.xmlsoap.org/ws/2005/04/discovery/ws-discovery.pdf) published by the WS-I organization.

Unlike mDNS which uses DNS-style queries, WSD uses SOAP XML messages encapsulated in UDP datagrams for discovery, with HTTP for subsequent metadata exchange.

**Sources**: [Microsoft WSDAPI docs](https://learn.microsoft.com/en-us/windows/win32/wsdapi/ws-discovery-specification-compliance), [WS-Discovery specification](https://specs.xmlsoap.org/ws/2005/04/discovery/ws-discovery.pdf)

### 1.2 Transport Layer

| Parameter | Value |
|-----------|-------|
| **Protocol** | SOAP over UDP |
| **Multicast Address (IPv4)** | `239.255.255.250` |
| **Multicast Address (IPv6)** | `FF02::C` (link-local) |
| **Port** | `3702` (both UDP and TCP) |
| **Metadata Exchange Port (HTTP)** | `5357` |
| **Metadata Exchange Port (HTTPS)** | `5358` |
| **Max Envelope Size** | 32KB (DPWS constraint) |
| **Max UDP Datagram** | 64KB |

**Key point**: Discovery messages (Hello, Bye, Probe, ProbeMatch, Resolve, ResolveMatch) use **UDP multicast on port 3702**. Metadata exchange (Get/GetResponse) uses **HTTP on port 5357** (or 5358 for HTTPS).

**Sources**: [Microsoft Additional WSD Functionality](https://learn.microsoft.com/en-us/windows/win32/wsdapi/additional-ws-discovery-functionality), [Device Discovery Protocols comparison](https://www.cnblogs.com/tong2357/p/18903256)

### 1.3 Message Types

WS-Discovery defines 8 message types organized into two categories:

#### Announcement Messages (one-way, unsolicited)
| Message | Direction | Transport | Purpose |
|---------|-----------|-----------|---------|
| **Hello** | Device -> Network | UDP multicast | Announce device is online |
| **Bye** | Device -> Network | UDP multicast | Announce device is going offline |

#### Search Messages (request/response pairs)
| Message | Direction | Transport | Purpose |
|---------|-----------|-----------|---------|
| **Probe** | Client -> Network | UDP multicast | Search for devices by type/scope |
| **ProbeMatch** | Device -> Client | UDP unicast | Response to Probe with device info |
| **Resolve** | Client -> Network | UDP multicast | Find current address of known device (by UUID) |
| **ResolveMatch** | Device -> Client | UDP unicast | Response to Resolve with current XAddrs |

#### Metadata Exchange Messages (over HTTP)
| Message | Direction | Transport | Purpose |
|---------|-----------|-----------|---------|
| **Get** | Client -> Device | HTTP POST | Request full device/service metadata |
| **GetResponse** | Device -> Client | HTTP response | Return device metadata XML |

**Sources**: [Microsoft Discovery and Metadata Exchange](https://learn.microsoft.com/en-us/windows/win32/wsdapi/discovery-and-metadata-exchange-message-patterns), [Microsoft WSDAPI Overview](https://learn.microsoft.com/en-us/windows/win32/wsdapi/about-web-services-for-devices)

### 1.4 Typical Discovery Flow for Scanner

```
┌──────────┐                              ┌──────────────┐
│  Client  │                              │ HP Scanner   │
│ (Python) │                              │ (WSD Host)   │
└────┬─────┘                              └──────┬───────┘
     │                                           │
     │  1. Hello (optional, on device boot)      │
     │  ◄────────────────────────────────────────┤  UDP multicast to 239.255.255.250:3702
     │  Contains: Device UUID, Types, Scopes     │  (No XAddrs in Microsoft's implementation)
     │                                           │
     │  2. Probe                                 │
     ├──────────────────────────────────────────►│  UDP multicast to 239.255.255.250:3702
     │  Contains: Type filter (e.g. ScanDevice)  │
     │                                           │
     │  3. ProbeMatch                            │
     │  ◄────────────────────────────────────────┤  UDP unicast to client's address
     │  Contains: Device UUID, Types, XAddrs     │
     │                                           │
     │  4. Get (Metadata Exchange)               │
     ├──────────────────────────────────────────►│  HTTP POST to XAddr (port 5357)
     │  WS-Transfer Get request                  │
     │                                           │
     │  5. GetResponse                           │
     │  ◄────────────────────────────────────────┤  HTTP response
     │  Contains: Full device/service XML        │
     │  (service types, endpoints, capabilities) │
     │                                           │
     │  6. Bye (optional, on device shutdown)    │
     │  ◄────────────────────────────────────────┤  UDP multicast
```

**Important notes**:
- Steps 1 (Hello) is optional; devices may not send it. Microsoft's WSDAPI implementation never includes XAddrs in Hello messages -- clients must follow up with a Resolve.
- Steps 2-3 (Probe/ProbeMatch) are the primary discovery mechanism.
- Steps 4-5 (Get/GetResponse) are needed to get full service metadata, including scanner-specific endpoints.
- The APP_MAX_DELAY (random delay before ProbeMatch) is 5000ms per spec, but Windows constrains it to 2500ms for firewall compatibility.

**Sources**: [Microsoft Discovery Patterns](https://learn.microsoft.com/en-us/windows/win32/wsdapi/discovery-and-metadata-exchange-message-patterns), [Microsoft Additional WSD Functionality](https://learn.microsoft.com/en-us/windows/win32/wsdapi/additional-ws-discovery-functionality)

### 1.5 SOAP Message Structure

All WSD messages use SOAP 1.2 envelopes. Example Probe message:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
               xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"
               xmlns:wsd="http://schemas.xmlsoap.org/ws/2005/04/discovery">
  <soap:Header>
    <wsa:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</wsa:To>
    <wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</wsa:Action>
    <wsa:MessageID>urn:uuid:UNIQUE-ID</wsa:MessageID>
  </soap:Header>
  <soap:Body>
    <wsd:Probe>
      <wsd:Types>pub:SmartCardDev:ScannerDevice</wsd:Types>
    </wsd:Probe>
  </soap:Body>
</soap:Envelope>
```

**Key XML elements**:
- `wsd:Types` -- Filter by device/service type (e.g., scanner, printer)
- `wsd:Scopes` -- Filter by scope (URI-based categorization)
- `wsd:XAddrs` -- Transport addresses (e.g., `http://192.168.1.100:5357/device-uuid`)
- `wsa:EndpointReference` -- Device UUID (logical address, independent of IP)

**Sources**: [WS-Discovery specification](https://specs.xmlsoap.org/ws/2005/04/discovery/ws-discovery.pdf), [Microsoft Get Metadata Exchange](https://learn.microsoft.com/en-us/windows/win32/wsdapi/get--metadata-exchange--http-request-and-message)

### 1.6 Two Discovery Modes

| Mode | Description | Use Case |
|------|-------------|----------|
| **Ad-Hoc** | Direct multicast between clients and devices on same subnet | Home/office LANs (our target) |
| **Managed** | Centralized Discovery Proxy; clients send unicast Probe to proxy | Enterprise networks, cross-subnet |

For the HP Scan Tool, only **Ad-Hoc mode** is relevant.

**Sources**: [Microsoft WCF Discovery Overview](https://learn.microsoft.com/en-us/dotnet/framework/wcf/feature-details/wcf-discovery-overview)

---

## 2. HP Scanner WSD Registration

### 2.1 WSD Device Architecture

HP network printers/MFPs implement the DPWS (Devices Profile for Web Services) stack. A single physical device hosts multiple logical services:

```
Physical Device (HP OfficeJet / LaserJet MFP)
├── Device UUID: urn:uuid:xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
├── Host: http://<device-ip>:5357/<device-uuid>
├── Service: Printer Service (print service type)
├── Service: Scanner Service (scan device type)
└── Service: Other services (fax, etc.)
```

Each device has:
- A **globally unique device UUID** (burned in at manufacture, never changes)
- **XAddrs** (transport addresses) that change when IP changes
- One or more **service types** (printer, scanner, etc.)

**Sources**: [Microsoft About WSD](https://learn.microsoft.com/en-us/windows/win32/wsdapi/about-web-services-for-devices)

### 2.2 Scanner Service Type

For WSD scanner discovery, the relevant device/service type is:

| Identifier | Value |
|-----------|-------|
| **Scan Device Type** | Used in Probe filter to find scanners |
| **WS-Scan Service** | The scanner service exposed via WSD |
| **HLK Test Specification** | `Device.Imaging.Scanner.WSD.WSScan` |

The Microsoft HLK test for WSD scanner discovery states:
> "The 'Probe' filters on the **ScanDeviceType** and the 'Resolve' message filters on the **Device UUID**."

This means:
1. Client sends Probe with ScanDeviceType in `<wsd:Types>`
2. Scanner responds with ProbeMatch containing its UUID and XAddrs
3. Client can then send Get (metadata exchange) to the XAddr to get full scanner service details

**Sources**: [Microsoft WSD Scan Discover](https://learn.microsoft.com/en-us/windows-hardware/test/hlk/testref/0cbb5c67-6f41-460d-846e-210bf2163921), [Microsoft WSD Scan Verify](https://learn.microsoft.com/en-us/windows-hardware/test/hlk/testref/ac904c9d-4117-4d89-ae72-476aa618255c)

### 2.3 HP-Specific Behavior

HP network printers (OfficeJet, LaserJet MFP, ENVY, etc.) typically:

1. **Register WSD services automatically** on the network when connected via Ethernet or Wi-Fi
2. **Listen on UDP port 3702** for WS-Discovery Probe messages
3. **Serve metadata on TCP port 5357** (HTTP) for Get/GetResponse exchanges
4. **Support both WSD and eSCL simultaneously** -- most modern HP MFPs are "dual-protocol" devices
5. **Use the same physical device** for both WSD-Scan and eSCL scan protocols

**Key insight from SaneOverNetwork documentation**: Network scanners generally fall into three categories:
- **eSCL only** -- discovered via mDNS (`_uscan._tcp`)
- **WSD only** -- discovered via WS-Discovery (common on Windows-oriented devices)
- **Both** -- support both protocols simultaneously (most modern HP MFPs)

**Sources**: [SaneOverNetwork Wiki](https://wiki.debian.org/SaneOverNetwork), [HP WSD Port Discussion](https://h30434.www3.hp.com/t5/Printer-Networking-and-Wireless/WSD-port-monitor-for-Printers-and-TCP-IP-ports-what-is-the/td-p/579705/page/2)

### 2.4 WSD vs WIA-USB for Scanners

Windows historically used two scanner connection paths:
- **WIA over USB** -- direct USB connection
- **WSD Scan** -- network connection via WS-Discovery + WS-Scan protocol

The WSD scan path is:
1. WS-Discovery (UDP 3702) to find the scanner
2. Metadata Exchange (HTTP 5357) to get scanner capabilities
3. WS-Scan protocol for actual scan operations

**Sources**: [Microsoft WIA with WSD](https://learn.microsoft.com/en-us/windows-hardware/drivers/image/wia-with-web-services-for-devices), [Microsoft Scan Service Schema](https://learn.microsoft.com/en-us/windows-hardware/drivers/image/scan-service--ws-scan--schema)

---

## 3. Python Implementations

### 3.1 WSDiscovery (PyPI: `WSDiscovery`)

**Package**: `WSDiscovery` on PyPI (latest version: 2.1.2, also documented at 1.1.1)

**Installation**:
```bash
pip install WSDiscovery
```

**API**:
```python
from WSDiscovery import WSDiscovery, QName, Scope

# Initialize and start
wsd = WSDiscovery()
wsd.start()

# Discover all services
services = wsd.searchServices()

# Filter and extract info
for service in services:
    epr = service.getEPR()        # Endpoint Reference (device UUID)
    xaddrs = service.getXAddrs()  # Transport addresses
    types = service.getTypes()    # Service types
    scopes = service.getScopes()  # Scopes
    print(f"Device: {epr}")
    print(f"Addresses: {xaddrs}")
    print(f"Types: {types}")

# Stop discovery
wsd.stop()
```

**Key methods**:
| Method | Description |
|--------|-------------|
| `start()` | Start the discovery engine (binds to UDP 3702) |
| `stop()` | Stop the discovery engine |
| `searchServices(timeout=...)` | Send Probe and wait for ProbeMatch responses |
| `publishService(types, scopes, xaddrs)` | Publish a local service |
| `getEPR()` | Get endpoint reference (UUID) from discovered service |
| `getXAddrs()` | Get transport addresses from discovered service |
| `getTypes()` | Get service types from discovered service |
| `getScopes()` | Get scopes from discovered service |

**CLI tool**: The package includes a `discover` command-line tool:
```bash
discover --help
```

**Assessment for HP Scan Tool**:
- **Pros**: Direct WS-Discovery implementation, can find WSD scanners, pure Python
- **Cons**: Primarily designed for ONVIF cameras; may need type filtering for scanners
- **Scanner filtering**: Check `service.getTypes()` for scanner-related type strings

**Sources**: [PyPI WSDiscovery](https://pypi.org/project/WSDiscovery/1.1.1/), [Python ONVIF camera discovery example](https://blog.csdn.net/weixin_43865152/article/details/107463554)

### 3.2 PyWSD

**Description**: A cross-platform Python toolkit for "Web Services for Devices" discovery and interaction. Supports Windows, Linux, and macOS.

**Key features**:
- Full WSD implementation (not just discovery, but also interaction)
- Uses raw sockets for multicast (bypasses Windows API restrictions)
- On Unix: uses native socket options or `libpcap`
- XML processing with `lxml`
- Includes scanner-specific modules (job creation, image retrieval)

**CLI tools**:
| Tool | Description |
|------|-------------|
| `wsd-discover` | Discover all WSD devices on network |
| `wsd-get-metadata` | Get device metadata XML |
| `wsd-scan-simple` | Trigger a scan and capture result |
| `wsd-print-testpage` | Print a test page |

**Assessment for HP Scan Tool**:
- **Pros**: Most complete Python WSD implementation; includes scanner interaction
- **Cons**: May have complex dependencies (libpcap on Linux); less widely used
- **Best fit**: If we need full WSD scan protocol (not just discovery)

**Sources**: [PyWSD documentation](https://wenku.csdn.net/doc/3oo5w38c2f)

### 3.3 wsdd (WS-Discovery Daemon)

**Repository**: `github.com/christgau/wsdd`

**Description**: A Python daemon that implements the WSD host side -- makes a machine discoverable as a WSD device by Windows. Primarily used for Samba servers to appear in Windows Network neighborhood.

**Key points**:
- Implements the **host** side (responds to Probes), not the **client** side
- Written in Python
- Used on Linux to make Samba shares visible in Windows Explorer
- **Not directly useful** for our scanner discovery use case (we need client-side discovery)

**Sources**: [wsdd documentation](https://blog.csdn.net/gitblog_01152/article/details/142088622)

### 3.4 Custom Implementation (Recommended Approach)

Given that:
1. The `WSDiscovery` PyPI package provides the core discovery protocol
2. The metadata exchange is straightforward HTTP + XML
3. Our existing `escl_engine.py` already handles XML parsing and HTTP requests

**Recommended**: Use `WSDiscovery` for the Probe/ProbeMatch phase, then implement custom HTTP Get for metadata exchange, reusing patterns from `escl_engine.py`.

---

## 4. WSD-to-eSCL Integration

### 4.1 The Core Question

When a scanner is discovered via WSD, how do we get the eSCL URL to use with our existing `escl_engine.py` scanning code?

### 4.2 Two Separate Protocols

**Critical insight**: WSD and eSCL are **independent protocols** that happen to describe the same physical scanner.

| Aspect | WSD | eSCL |
|--------|-----|------|
| **Discovery** | WS-Discovery (UDP 3702) | mDNS (`_uscan._tcp`) |
| **Metadata** | HTTP Get to port 5357 | HTTP GET to `/eSCL/ScannerCapabilities` |
| **Scanning** | WS-Scan protocol | HTTP POST to `/eSCL/ScanJobs` |
| **URL format** | `http://<ip>:5357/<uuid>` | `http://<ip>:80/eSCL/` |

### 4.3 Getting eSCL URL from WSD Discovery

The path from WSD discovery to eSCL scanning:

```
1. WSD Probe (UDP 3702)
   └── ProbeMatch returns XAddrs: http://192.168.1.100:5357/<device-uuid>

2. WSD Get (HTTP POST to XAddr)
   └── GetResponse returns device metadata XML containing:
       ├── Device UUID
       ├── Service types (printer, scanner)
       ├── Scanner service endpoint
       └── Device IP address

3. Extract IP address from XAddrs or metadata
   └── Construct eSCL URL: http://<ip>:80/eSCL/

4. Probe eSCL (existing escl_engine.py probe_escl())
   └── Verify eSCL service is available at http://<ip>:80/eSCL/ScannerStatus
```

**Key steps for integration**:
1. Use WSD to discover the device and get its IP address
2. Use the IP to construct a candidate eSCL URL: `http://<ip>/eSCL/`
3. Call existing `probe_escl(ip, port)` to verify eSCL is available
4. If eSCL is available, use existing scanning code unchanged

### 4.4 Alternative: Use WSD Scanning Directly

If the scanner only supports WSD (no eSCL), we would need:
1. WSD discovery (Probe/ProbeMatch)
2. Metadata exchange (Get/GetResponse) to get scanner capabilities
3. WS-Scan protocol for scan job creation and retrieval

This is significantly more complex than eSCL and would require implementing the WS-Scan SOAP protocol. **Not recommended** as a first step.

### 4.5 Practical Integration Strategy

```python
# In wsd_engine.py

def discover_scanners_wsd(timeout=4.0) -> list[ScannerInfo]:
    """Discover scanners via WS-Discovery, then probe for eSCL."""
    from WSDiscovery import WSDiscovery

    wsd = WSDiscovery()
    wsd.start()
    services = wsd.searchServices(timeout=timeout)
    wsd.stop()

    scanners = []
    for service in services:
        # Extract IP from XAddrs
        xaddrs = service.getXAddrs()
        for xaddr in xaddrs:
            ip = extract_ip_from_xaddr(xaddr)
            if ip:
                # Probe for eSCL using existing code
                escl_url = probe_escl(ip)
                if escl_url:
                    scanner = ScannerInfo(
                        name=service.getEPR(),
                        ip=ip,
                        escl_url=escl_url,
                        vendor="HP",  # Could detect from metadata
                    )
                    scanners.append(scanner)
    return scanners
```

**Sources**: [SaneOverNetwork](https://wiki.debian.org/SaneOverNetwork), [Microsoft Discovery Patterns](https://learn.microsoft.com/en-us/windows/win32/wsdapi/discovery-and-metadata-exchange-message-patterns)

---

## 5. Practical Considerations

### 5.1 Network Configuration

| Parameter | Value | Notes |
|-----------|-------|-------|
| **Discovery transport** | UDP multicast | Port 3702 |
| **Multicast group** | 239.255.255.250 | Same as SSDP/UPnP |
| **Metadata transport** | HTTP unicast | Port 5357 (TCP) |
| **Scope** | Link-local (same subnet) | Multicast does not route |
| **IPv6 support** | FF02::C | Link-local only |

### 5.2 Firewall Implications

**Windows Firewall**:
- WSD requires the "Network Discovery" firewall rule group to be enabled
- UDP port 3702 must be allowed for multicast discovery
- TCP port 5357 must be allowed for metadata exchange
- Microsoft documentation states: "Windows Firewall requires that the multicast request/unicast response model for UDP will only work within the 4 second firewall window"

**Linux (iptables/ufw)**:
- Allow UDP 3702 inbound/outbound for multicast
- Allow TCP 5357 inbound for metadata responses
- Example: `ufw allow 3702/udp && ufw allow 5357/tcp`

**Router/Switch considerations**:
- IGMP snooping must be enabled for efficient multicast
- Some enterprise switches block multicast by default
- WSD does NOT work across subnets (in Ad-Hoc mode)

**Sources**: [Microsoft Firewall Settings](https://learn.microsoft.com/en-us/windows/win32/wsdapi/inspecting-adapter-and-firewall-settings), [Microsoft Additional WSD Functionality](https://learn.microsoft.com/en-us/windows/win32/wsdapi/additional-ws-discovery-functionality)

### 5.3 WSD vs mDNS Comparison

| Aspect | WS-Discovery (WSD) | mDNS (Bonjour/Zeroconf) |
|--------|--------------------|-------------------------|
| **Transport** | SOAP over UDP | DNS over UDP |
| **Port** | 3702 (discovery) + 5357 (metadata) | 5353 |
| **Multicast address** | 239.255.255.250 | 224.0.0.251 |
| **Message format** | SOAP XML | DNS records |
| **Discovery speed** | 2-5 seconds (due to Probe timeout) | < 1 second (DNS-style) |
| **Reliability** | Good (SOAP retransmission) | Good (DNS retransmission) |
| **Cross-platform** | Native on Windows, needs library on Linux/Mac | Native on macOS/Linux, needs library on Windows |
| **Scanner service type** | ScanDeviceType (WSD) | `_uscan._tcp` (eSCL) |
| **Metadata richness** | Very rich (full device model) | Moderate (TXT records) |
| **Firewall issues** | Common (needs port 3702 + 5357) | Rare (well-known ports) |
| **HP scanner support** | Excellent (Windows-native) | Excellent (AirPrint/eSCL) |
| **Library maturity (Python)** | Moderate (WSDiscovery, PyWSD) | High (python-zeroconf) |

### 5.4 Reliability and Speed

**Discovery latency**:
- mDNS: Typically < 1 second (DNS query/response pattern)
- WSD: 2-5 seconds (Probe + random APP_MAX_DELAY + ProbeMatch)
- Combined: Running both in parallel adds no latency (use the first result)

**Reliability**:
- Both protocols are reliable on same-subnet LANs
- WSD may fail more often due to firewall blocking UDP 3702
- mDNS may fail on networks with mDNS-snooping issues
- Running both in parallel provides redundancy

**Duplicate handling**:
- Same scanner may be discovered by both mDNS and WSD
- Must deduplicate by IP address (both methods yield the device IP)
- The existing `discover_scanners()` already deduplicates by IP

### 5.5 Port Conflict Considerations

- Port 3702 is shared by all WSD/UPnP devices on the network
- Multiple WSD applications can share the port via SO_REUSEADDR
- Microsoft's WSDAPI uses port sharing; binding exclusively may block other applications
- The `WSDiscovery` Python library handles port sharing

**Sources**: [Microsoft Additional WSD Functionality](https://learn.microsoft.com/en-us/windows/win32/wsdapi/additional-ws-discovery-functionality)

---

## 6. Implementation Recommendations

### 6.1 Architecture: Parallel Discovery

```python
# In the main application or a new discovery coordinator

def discover_all_scanners(timeout=4.0) -> list[ScannerInfo]:
    """Discover scanners via both mDNS and WSD in parallel."""
    import concurrent.futures

    results = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        # Submit both discovery tasks
        mdns_future = executor.submit(discover_scanners_mdns, timeout)
        wsd_future = executor.submit(discover_scanners_wsd, timeout)

        # Collect results
        for future in (mdns_future, wsd_future):
            try:
                results.extend(future.result())
            except Exception as e:
                logging.warning("Discovery method failed: %s", e)

    # Deduplicate by IP
    seen_ips = set()
    unique = []
    for scanner in results:
        if scanner.ip not in seen_ips:
            seen_ips.add(scanner.ip)
            unique.append(scanner)

    return unique
```

### 6.2 File Structure

```
HP_Scan_Tool/
├── escl_engine.py      # Existing: mDNS discovery + eSCL scanning
├── wsd_engine.py       # NEW: WSD discovery + optional WSD scanning
├── hp_scan_gui.py      # Modified: use combined discovery
└── wia_engine.py       # Existing: WIA scanning (Windows)
```

### 6.3 wsd_engine.py Implementation Plan

1. **Phase 1: WSD Discovery Only** (recommended first step)
   - Use `WSDiscovery` library for Probe/ProbeMatch
   - Extract IP from XAddrs
   - Call existing `probe_escl()` to get eSCL URL
   - Return `ScannerInfo` objects compatible with existing code
   - **Benefit**: Adds WSD-discovered scanners to the list without changing scanning logic

2. **Phase 2: WSD Metadata Enrichment** (optional)
   - After WSD discovery, send Get to XAddr for full metadata
   - Parse device model, serial number, capabilities from WSD metadata
   - Enrich `ScannerInfo` with data not available from mDNS

3. **Phase 3: WSD-Only Scanning** (only if needed)
   - Implement WS-Scan protocol for scanners that don't support eSCL
   - This is complex and should only be done if Phase 1/2 don't cover target devices

### 6.4 Dependencies

```
# Add to requirements.txt
WSDiscovery>=2.1.2
```

No additional system dependencies needed on Windows. On Linux, the `WSDiscovery` library should work without libpcap for basic discovery.

### 6.5 Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| WSD blocked by firewall | High | Medium | Fall back to mDNS; warn user |
| WSDiscovery library unmaintained | Medium | High | Pin version; be ready to fork |
| WSD discovery too slow | Low | Low | Run in parallel with mDNS |
| Duplicate scanners | High | Low | Deduplicate by IP (already done) |
| Port 3702 conflict | Low | Medium | Use SO_REUSEADDR; handle gracefully |

---

## 7. Sources

### Primary Sources (Protocol Specifications)
- [WS-Discovery Specification (PDF)](https://specs.xmlsoap.org/ws/2005/04/discovery/ws-discovery.pdf) -- The official OASIS specification
- [Devices Profile for Web Services (DPWS)](https://specs.xmlsoap.org/ws/2006/02/devprof/) -- Device profile built on WS-Discovery
- [WS-Transfer Specification](https://specs.xmlsoap.org/ws/2004/09/transfer/WS-Transfer.pdf) -- Used for metadata exchange (Get/GetResponse)

### Microsoft Documentation (WSDAPI)
- [About Web Services on Devices](https://learn.microsoft.com/en-us/windows/win32/wsdapi/about-web-services-for-devices) -- Overview of WSD architecture
- [WS-Discovery Specification Compliance](https://learn.microsoft.com/en-us/windows/win32/wsdapi/ws-discovery-specification-compliance) -- How Windows implements WSD
- [Discovery and Metadata Exchange Message Patterns](https://learn.microsoft.com/en-us/windows/win32/wsdapi/discovery-and-metadata-exchange-message-patterns) -- Message flow diagram and sequence
- [Additional WS-Discovery Functionality](https://learn.microsoft.com/en-us/windows/win32/wsdapi/additional-ws-discovery-functionality) -- Port numbers, XAddrs behavior, APP_MAX_DELAY
- [Get (Metadata Exchange) HTTP Request](https://learn.microsoft.com/en-us/windows/win32/wsdapi/get--metadata-exchange--http-request-and-message) -- Get message format
- [WSD Scan Discover (HLK Test)](https://learn.microsoft.com/en-us/windows-hardware/test/hlk/testref/0cbb5c67-6f41-460d-846e-210bf2163921) -- ScanDeviceType probe test
- [WSD Scan Verify (HLK Test)](https://learn.microsoft.com/en-us/windows-hardware/test/hlk/testref/ac904c9d-4117-4d89-ae72-476aa618255c) -- WS-Scan protocol verification
- [Scan Service (WS-Scan) Schema](https://learn.microsoft.com/en-us/windows-hardware/drivers/image/scan-service--ws-scan--schema) -- Scanner service definition
- [WCF Discovery Overview](https://learn.microsoft.com/en-us/dotnet/framework/wcf/feature-details/wcf-discovery-overview) -- Managed vs Ad-Hoc modes

### Python Libraries
- [WSDiscovery on PyPI](https://pypi.org/project/WSDiscovery/1.1.1/) -- Primary Python WSD library
- [PyWSD documentation](https://wenku.csdn.net/doc/3oo5w38c2f) -- Full WSD toolkit with scanner support
- [wsdd (WS-Discovery Daemon)](https://blog.csdn.net/gitblog_01152/article/details/142088622) -- Host-side WSD for Samba

### Scanner Discovery References
- [SaneOverNetwork](https://wiki.debian.org/SaneOverNetwork) -- eSCL/WSD scanner categories
- [sane-airscan](https://github.com/alexpevzner/sane-airscan) -- Reference implementation for AirScan/eSCL
- [eSCL Debian Wiki](https://wiki.debian.org/eSCL) -- eSCL scanner documentation

### Protocol Comparisons
- [Device Discovery Protocols](https://www.cnblogs.com/tong2357/p/18903256) -- WSD vs mDNS vs SSDP comparison
- [WS-Discovery Wireshark Analysis](https://blog.csdn.net/lstm7chronicler/article/details/153103804) -- Packet-level analysis
- [WS-Discovery Protocol Deep Dive](https://blog.csdn.net/weixin_29306317/article/details/159606896) -- Protocol architecture

### Python Usage Examples
- [Python ONVIF Camera Discovery](https://blog.csdn.net/weixin_43865152/article/details/107463554) -- WSDiscovery API usage
- [ONVIF WS-Discovery Protocol](https://www.cnblogs.com/-jimmy-/articles/14867849.html) -- Probe/ProbeMatch flow

---

## Appendix A: Key Constants for Implementation

```python
# WS-Discovery constants
WSD_MULTICAST_ADDR = "239.255.255.250"
WSD_PORT = 3702
WSD_METADATA_PORT = 5357
WSD_METADATA_PORT_HTTPS = 5358

# WS-Discovery SOAP namespaces
WSD_NS = "http://schemas.xmlsoap.org/ws/2005/04/discovery"
WSA_NS = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
SOAP_NS = "http://www.w3.org/2003/05/soap-envelope"

# Scanner device type (for Probe filter)
# Exact value depends on device; common patterns:
# - "pub:SmartCardDev:ScannerDevice"
# - ScanDeviceType (as referenced in Microsoft HLK tests)
# - May need to probe without type filter and check responses

# eSCL paths (for post-WSD probing)
ESCL_PATHS = ["/eSCL/ScannerStatus", "/ScannerStatus"]
```

## Appendix B: Sample WSD Probe SOAP Message

```xml
<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope
    xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
    xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"
    xmlns:wsd="http://schemas.xmlsoap.org/ws/2005/04/discovery">
  <soap:Header>
    <wsa:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</wsa:To>
    <wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</wsa:Action>
    <wsa:MessageID>urn:uuid:a1b2c3d4-e5f6-7890-abcd-ef1234567890</wsa:MessageID>
  </soap:Header>
  <soap:Body>
    <wsd:Probe>
      <!-- Optional: filter by scanner type -->
      <!-- <wsd:Types>pub:SmartCardDev:ScannerDevice</wsd:Types> -->
    </wsd:Probe>
  </soap:Body>
</soap:Envelope>
```

## Appendix C: Sample ProbeMatch Response

```xml
<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope
    xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
    xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"
    xmlns:wsd="http://schemas.xmlsoap.org/ws/2005/04/discovery">
  <soap:Header>
    <wsa:To>http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</wsa:To>
    <wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches</wsa:Action>
    <wsa:RelatesTo>urn:uuid:a1b2c3d4-e5f6-7890-abcd-ef1234567890</wsa:RelatesTo>
  </soap:Header>
  <soap:Body>
    <wsd:ProbeMatches>
      <wsd:ProbeMatch>
        <wsa:EndpointReference>
          <wsa:Address>urn:uuid:device-uuid-here</wsa:Address>
        </wsa:EndpointReference>
        <wsd:Types>pub:SmartCardDev:ScannerDevice</wsd:Types>
        <wsd:XAddrs>http://192.168.1.100:5357/device-uuid-here</wsd:XAddrs>
      </wsd:ProbeMatch>
    </wsd:ProbeMatches>
  </soap:Body>
</soap:Envelope>
```
