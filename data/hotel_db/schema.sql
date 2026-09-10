-- GENERATED from data/aether_hotel.db by scripts/dump_hotel_db.py.
-- Do not edit by hand: change the database, then regenerate.
CREATE TABLE guests(
 guest_id INTEGER PRIMARY KEY, full_name TEXT NOT NULL, phone TEXT, email TEXT,
 language TEXT DEFAULT 'English'
);
CREATE TABLE hotel(
 hotel_id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT,
 phone TEXT, email TEXT, address TEXT, check_in_time TEXT NOT NULL,
 check_out_time TEXT NOT NULL, currency TEXT NOT NULL DEFAULT 'INR'
);
CREATE TABLE hotel_policies (
    topic        TEXT PRIMARY KEY,   -- machine key: parking, wifi, pets, ...
    available    INTEGER NOT NULL,   -- 0 or 1. 0 means "we do not offer this", which is an answer
    fee_inr      REAL,               -- NULL when free or not applicable
    hours        TEXT,               -- "HH:MM-HH:MM" or "24 hours"; NULL when not time-bound
    limit_hours  INTEGER,            -- notice period, e.g. free cancellation up to N hours before
    options      TEXT                -- comma-separated machine keys, e.g. "card,cash,upi"
);
CREATE TABLE hotel_services(
 service_id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, description TEXT,
 availability TEXT, phone_extension TEXT
);
CREATE TABLE menu_categories(
 category_id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, display_order INTEGER NOT NULL
);
CREATE TABLE menu_items(
 item_id INTEGER PRIMARY KEY, category_id INTEGER NOT NULL REFERENCES menu_categories(category_id),
 name TEXT NOT NULL, description TEXT, price_inr REAL NOT NULL,
 vegetarian INTEGER NOT NULL DEFAULT 0, vegan INTEGER NOT NULL DEFAULT 0,
 contains_egg INTEGER NOT NULL DEFAULT 0, allergens TEXT, available INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE reservations(
 reservation_id INTEGER PRIMARY KEY, guest_id INTEGER NOT NULL REFERENCES guests(guest_id),
 room_id INTEGER NOT NULL REFERENCES rooms(room_id),
 check_in TEXT NOT NULL, check_out TEXT NOT NULL,
 adults INTEGER NOT NULL DEFAULT 1, children INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL CHECK(status IN('confirmed','checked_in','checked_out','cancelled')),
 special_requests TEXT, created_at TEXT NOT NULL
);
CREATE TABLE restaurant_order_items(
 order_item_id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES restaurant_orders(order_id) ON DELETE CASCADE,
 item_id INTEGER NOT NULL REFERENCES menu_items(item_id), quantity INTEGER NOT NULL,
 unit_price_inr REAL NOT NULL
);
CREATE TABLE restaurant_orders(
 order_id INTEGER PRIMARY KEY, guest_id INTEGER REFERENCES guests(guest_id),
 room_id INTEGER REFERENCES rooms(room_id),
 order_source TEXT NOT NULL CHECK(order_source IN('room_service','restaurant')),
 status TEXT NOT NULL CHECK(status IN('pending','preparing','served','cancelled')),
 created_at TEXT NOT NULL
);
CREATE TABLE room_types(
 room_type_id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, description TEXT,
 base_rate_inr REAL NOT NULL, max_guests INTEGER NOT NULL, amenities TEXT NOT NULL
);
CREATE TABLE rooms(
 room_id INTEGER PRIMARY KEY, room_number TEXT UNIQUE NOT NULL,
 room_type_id INTEGER NOT NULL REFERENCES room_types(room_type_id),
 floor INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN('available','occupied','reserved','maintenance','housekeeping')),
 view TEXT, notes TEXT
);
CREATE TABLE service_requests(
 request_id INTEGER PRIMARY KEY, guest_id INTEGER REFERENCES guests(guest_id),
 room_id INTEGER REFERENCES rooms(room_id), service_id INTEGER NOT NULL REFERENCES hotel_services(service_id),
 details TEXT, status TEXT NOT NULL CHECK(status IN('requested','in_progress','completed','cancelled')),
 created_at TEXT NOT NULL
);
CREATE TABLE table_bookings (
    booking_id   INTEGER PRIMARY KEY,
    guest_id     INTEGER REFERENCES guests(guest_id),
    party_size   INTEGER NOT NULL CHECK(party_size > 0),
    sitting      TEXT NOT NULL,          -- "HH:MM", the hour the table is held from
    booked_for   TEXT NOT NULL,          -- ISO date
    status       TEXT NOT NULL CHECK(status IN('confirmed','seated','cancelled')),
    created_at   TEXT NOT NULL
);
CREATE VIEW available_rooms AS
SELECT r.room_number, rt.name room_type, rt.base_rate_inr, rt.max_guests, r.floor, r.view, rt.amenities
FROM rooms r JOIN room_types rt ON rt.room_type_id=r.room_type_id WHERE r.status='available';
CREATE VIEW available_menu AS
SELECT c.name category, m.item_id, m.name, m.description, m.price_inr,
       m.vegetarian, m.vegan, m.allergens
FROM menu_items m JOIN menu_categories c ON c.category_id=m.category_id WHERE m.available=1;
CREATE VIEW current_reservations AS
SELECT res.reservation_id,g.full_name guest_name,r.room_number,rt.name room_type,
       res.check_in,res.check_out,res.status
FROM reservations res JOIN guests g ON g.guest_id=res.guest_id
JOIN rooms r ON r.room_id=res.room_id JOIN room_types rt ON rt.room_type_id=r.room_type_id
WHERE res.status IN('confirmed','checked_in');
