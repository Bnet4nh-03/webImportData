// Cột của bảng, theo đúng thứ tự các bước "Thêm Repair mới" của bot PCBA-CCVN.
// `from`: cột được AI điền từ tem; cột không có `from` để trống chờ nhập tay.
// `options`: danh sách lựa chọn giống nút bấm của bot (ô dạng chọn, để trống = bỏ trống).

export const BOARDS = [
  "12010000047 - OCPP_SINGLE_V3.9",
  "12070000231 - Slot Control Board - BSS 6/12 Slot",
  "12030000090 - Main Controller Board (PCBA)",
  "12010000065 - AC PCBA - AC 11Kw",
  "12070000206 - Power Board-HV Kern V1.4",
  "12070000228 - Main Control Board - BSS 6 Slot",
  "12030000075 - SECC Core V1.8.2 - Core 60/80/120/150 Kw",
  "12070000170 - SAS_V1.3",
  "12070000186 - SAS Single V1.4",
  "12030000064 - SECC Board V1.6- Kern",
  "12030000054 - SECC Core V1.8 - Core 60/80/120/150 Kw",
  "12030000067 - SECC Core V1.8.1 - Core 60/80/120/150 Kw",
  "12070000188 - SAS Double V1.1",
  "12070000176 - Power Board-HV Kern V1.0",
  "12010000075 - OCPP AC 7kw and AC 22Kw V3.8",
  "12030000025 - HV Core V2.7 - Core 60/80/120/150Kw",
  "12010000066 - AC PCBA - AC 22Kw (Old Version)",
  "12070000153 - HV Core V2.8 - Core 60/80/120/150Kw",
  "12010000076 - AC PCBA - AC 7Kw",
  "12010000091 - AC PCBA - AC 22Kw V2.6.1",
  "12010000086 - OCPP AC 7kw and AC 22Kw (New Version)",
  "12030000059 - SECC Board V1.5 - Kern",
  "12030000053 - SECC Board V1.2 - Kern",
  "12030000001 - SECC Core V1.7 - Core 60/80/120/150 Kw",
  "BRD_OCPP_THREE_B45EF5 - OCPP_Three_V2.6_22KW",
  "BRD_OCPP_THREE_165313 - OCPP_Three_V2.6_7KW",
  "BRD_OCPP_THREE_6A3FB4 - OCPP_Three_V2.6.1_22KW",
  "12050000012 - WIFI ETH V1.0",
];

export const CONTRACTORS = ["EC", "ES", "ITS", "HTE", "SE HN", "SE HCM"];
export const RESULTS = ["PASS", "FAIL", "Discard"];
// Hệ thống PCBA: Kiểm tra tại trạm OK / FAIL / Not used / Not tested; Pending = để trống (chờ kiểm tra tại trạm).
export const STATION_STATUS = ["Pending", "OK", "FAIL", "Not used", "Not tested"];
// Lý do không có SN bo mạch (đúng 4 lựa chọn của trang Nhận bo mạch).
export const SN_REASONS = ["Không có tem", "Tem hỏng", "Tem không đọc được", "Lý do khác"];
export const VERIFY_STATUS = ["Pending", "PASS"];

// Ảnh kèm theo cột: kind gửi lên server, field lưu trên dòng, tên hiển thị.
export const PHOTO = {
  after: { kind: "after", field: "photos", name: "ảnh sau sửa" },
  verify: { kind: "verify", field: "vphotos", name: "ảnh xác minh lỗi" },
};

// Gợi ý Board theo charge_box_model của ticket (chỉ để xếp board hay gặp lên đầu danh sách,
// không tự chọn board và không suy ra kết quả kiểm tra). Sửa danh sách này khi có model/board mới.
const AC22 = ["12010000091", "12010000066", "12010000075", "12010000086", "BRD_OCPP_THREE_B45EF5", "BRD_OCPP_THREE_6A3FB4", "12050000012"];
const AC7 = ["12010000076", "12010000075", "12010000086", "BRD_OCPP_THREE_165313", "12050000012"];
const CORE = ["12030000075", "12030000067", "12030000054", "12030000001", "12070000153", "12030000025", "12010000047", "12050000012"];
const KERN = ["12030000064", "12030000059", "12030000053", "12070000206", "12070000176", "12010000047", "12050000012"];
export const BOARD_RULES = [
  { test: /SwapMax/i, boards: ["12070000231", "12070000228", "12030000090"] },
  { test: /SAS/i, boards: ["12070000170", "12070000186", "12070000188"] },
  { test: /AC006T222|22\s*KW/i, boards: AC22 },
  { test: /AC006T27|AC7T|AC0037|0?7\s*KW/i, boards: AC7 },
  { test: /11\s*KW/i, boards: ["12010000065", "12010000075", "12010000086"] },
  { test: /Kern/i, boards: KERN },
  { test: /CORE|ADC0|CC_\d+kW|DC0/i, boards: CORE },
];

/** Board gợi ý cho một model (đúng chuỗi trong BOARDS). */
export function boardsForModel(model) {
  const rule = model && BOARD_RULES.find((r) => r.test.test(model));
  if (!rule) return [];
  return rule.boards.map((code) => BOARDS.find((b) => b.startsWith(code + " "))).filter(Boolean);
}

// `ai`: cột AI đọc từ tem và được đối chiếu với ccts_tickets.db (`db` là trường trong DB).
// `options`: danh sách chọn giống nút bấm của bot. `photo`: có nút chụp ảnh kèm theo.
export const COLUMNS = [
  { key: "board", label: "Board", options: BOARDS, wide: true, group: "repair" },
  // SN của BO MẠCH (quét QR trên bo) — khác SN trụ sạc trên tem. Tra trên hệ thống PCBA: đã có bo chưa, phiếu mở.
  { key: "board_sn", label: "Check SN", qr: true, wide: true, group: "repair" },
  { key: "sn_missing_reason", label: "Lý do không có SN", options: SN_REASONS, group: "repair" },
  { key: "contractor", label: "Contractor", options: CONTRACTORS, group: "repair" },
  { key: "test_tool_result", label: "test_tool_result", options: RESULTS, group: "repair" },
  { key: "failure_cause", label: "failure_cause", wide: true, group: "repair" },
  { key: "disposition", label: "disposition", wide: true, group: "repair" },
  { key: "after_repair_result", label: "after_repair_result", options: RESULTS, photo: PHOTO.after, group: "repair" },
  { key: "charging_station_test_status", label: "charging_station_test_status", options: STATION_STATUS, group: "repair" },
  { key: "ticket_id", label: "Ticket ID", ai: "Ticket", db: "ccts_ticket_id" },
  { key: "sn", label: "SN", ai: "S/N Trụ Sạc", db: "charge_box_id", wide: true },
  { key: "date_onsite", label: "Date onsite", ai: "Ngày Thay Thế", db: "onSiteCompleteTime" },
  { key: "original_phenomenon", label: "Original phenomenon", ai: "Mã lỗi + Mô Tả Lỗi", db: "error_desc", wide: true, long: true },
  { key: "station_code", label: "Station code", ai: "Mã Trạm", db: "station_code" },
  // Bot: "Gửi ảnh Failure verification photo (ảnh xác minh hiện tượng lỗi thực tế)"
  { key: "failure_verification", label: "Failure verification photo", options: VERIFY_STATUS, photo: PHOTO.verify, group: "repair" },
];
export const COMPARE_KEYS = COLUMNS.filter((c) => c.db).map((c) => c.key);

export const DATE_FORMATS = { mdy: "M/D/YYYY (8/1/2026)", iso: "YYYY-MM-DD (2026-08-01)", dmy: "DD/MM/YYYY (01/08/2026)" };
