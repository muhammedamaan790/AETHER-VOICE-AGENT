-- AETHER Hotel synthetic seed. Recreates the database contents.
PRAGMA foreign_keys=OFF;
BEGIN TRANSACTION;
-- hotel
INSERT INTO hotel (hotel_id, name, description, phone, email, address, check_in_time, check_out_time, currency) VALUES (1, 'AETHER Grand Hotel', 'Fictional full-service city hotel used as the AETHER demo environment.', '+91 44 4000 1000', 'frontdesk@aether.example', 'Chennai, Tamil Nadu, India', '14:00', '12:00', 'INR');
-- room_types
INSERT INTO room_types (room_type_id, name, description, base_rate_inr, max_guests, amenities) VALUES (1, 'Standard King', 'Comfortable king room for business and leisure stays.', 6500.0, 2, 'King bed, Wi-Fi, air conditioning, TV, work desk');
INSERT INTO room_types (room_type_id, name, description, base_rate_inr, max_guests, amenities) VALUES (2, 'Standard Twin', 'Two-bed room suitable for two guests.', 6500.0, 2, 'Two twin beds, Wi-Fi, air conditioning, TV, work desk');
INSERT INTO room_types (room_type_id, name, description, base_rate_inr, max_guests, amenities) VALUES (3, 'Deluxe King', 'Larger king room with upgraded furnishings.', 8500.0, 2, 'King bed, Wi-Fi, air conditioning, smart TV, minibar, city view');
INSERT INTO room_types (room_type_id, name, description, base_rate_inr, max_guests, amenities) VALUES (4, 'Executive Suite', 'Separate sleeping and living areas.', 12500.0, 3, 'King bed, living room, Wi-Fi, smart TV, minibar, city view, breakfast');
INSERT INTO room_types (room_type_id, name, description, base_rate_inr, max_guests, amenities) VALUES (5, 'Family Suite', 'Spacious suite designed for families.', 15000.0, 4, 'King bed, twin sofa beds, Wi-Fi, two TVs, minibar, breakfast');
-- rooms
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (1, '101', 1, 1, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (2, '102', 1, 1, 'occupied', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (3, '103', 2, 1, 'available', 'Garden', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (4, '104', 2, 1, 'housekeeping', 'Garden', 'Being prepared');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (5, '201', 3, 2, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (6, '202', 3, 2, 'reserved', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (7, '203', 3, 2, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (8, '204', 3, 2, 'maintenance', 'City', 'AC service');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (9, '301', 4, 3, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (10, '302', 4, 3, 'occupied', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (11, '401', 5, 4, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (12, '402', 5, 4, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (13, '105', 1, 1, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (14, '106', 2, 1, 'available', 'Garden', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (15, '107', 1, 1, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (16, '108', 2, 1, 'available', 'Garden', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (17, '109', 1, 1, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (18, '110', 2, 1, 'available', 'Garden', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (19, '205', 3, 2, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (20, '206', 3, 2, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (21, '207', 3, 2, 'reserved', 'City', 'Upcoming arrival');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (22, '208', 3, 2, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (23, '209', 3, 2, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (24, '210', 3, 2, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (25, '303', 3, 3, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (26, '304', 3, 3, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (27, '305', 3, 3, 'occupied', 'City', 'In-house guest');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (28, '306', 3, 3, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (29, '307', 4, 3, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (30, '308', 4, 3, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (31, '309', 4, 3, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (32, '310', 4, 3, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (33, '403', 4, 4, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (34, '404', 4, 4, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (35, '405', 4, 4, 'housekeeping', 'City', 'Turnover in progress');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (36, '406', 4, 4, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (37, '407', 5, 4, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (38, '408', 5, 4, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (39, '409', 5, 4, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (40, '410', 5, 4, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (41, '501', 5, 5, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (42, '502', 5, 5, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (43, '503', 5, 5, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (44, '504', 5, 5, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (45, '505', 5, 5, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (46, '506', 5, 5, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (47, '507', 5, 5, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (48, '508', 5, 5, 'maintenance', 'City', 'Routine inspection');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (49, '509', 5, 5, 'available', 'City', '');
INSERT INTO rooms (room_id, room_number, room_type_id, floor, status, view, notes) VALUES (50, '510', 5, 5, 'available', 'City', '');
-- guests
INSERT INTO guests (guest_id, full_name, phone, email, language) VALUES (1, 'Eleanor Whitfield', '+44 20 7946 0118', 'eleanor@example.test', 'English');
INSERT INTO guests (guest_id, full_name, phone, email, language) VALUES (2, 'Arjun Mehta', '+91 90000 10002', 'arjun@example.test', 'English');
INSERT INTO guests (guest_id, full_name, phone, email, language) VALUES (3, 'Sofia Martin', '+33 1 4000 1003', 'sofia@example.test', 'French');
INSERT INTO guests (guest_id, full_name, phone, email, language) VALUES (4, 'Daniel Chen', '+65 6000 1004', 'daniel@example.test', 'English');
-- reservations
INSERT INTO reservations (reservation_id, guest_id, room_id, check_in, check_out, adults, children, status, special_requests, created_at) VALUES (1001, 1, 10, '2026-09-07', '2026-09-12', 1, 0, 'checked_in', 'Late checkout requested', '2026-08-30T10:00:00');
INSERT INTO reservations (reservation_id, guest_id, room_id, check_in, check_out, adults, children, status, special_requests, created_at) VALUES (1002, 2, 2, '2026-09-06', '2026-09-10', 2, 0, 'checked_in', 'Extra towels', '2026-08-31T09:00:00');
INSERT INTO reservations (reservation_id, guest_id, room_id, check_in, check_out, adults, children, status, special_requests, created_at) VALUES (1003, 3, 6, '2026-09-15', '2026-09-18', 2, 0, 'confirmed', 'Vegetarian breakfast', '2026-09-01T11:00:00');
-- menu_categories
INSERT INTO menu_categories (category_id, name, display_order) VALUES (1, 'Starters', 1);
INSERT INTO menu_categories (category_id, name, display_order) VALUES (2, 'Mains', 2);
INSERT INTO menu_categories (category_id, name, display_order) VALUES (3, 'Vegetarian Mains', 3);
INSERT INTO menu_categories (category_id, name, display_order) VALUES (4, 'Desserts', 4);
INSERT INTO menu_categories (category_id, name, display_order) VALUES (5, 'Drinks', 5);
-- menu_items
INSERT INTO menu_items (item_id, category_id, name, description, price_inr, vegetarian, vegan, contains_egg, allergens, available) VALUES (1, 1, 'Chicken Kebab', 'Grilled chicken with herbs and yogurt dip.', 420.0, 0, 0, 0, 'Dairy', 1);
INSERT INTO menu_items (item_id, category_id, name, description, price_inr, vegetarian, vegan, contains_egg, allergens, available) VALUES (2, 1, 'Paneer Tikka', 'Char-grilled paneer with peppers and spices.', 360.0, 1, 0, 0, 'Dairy', 1);
INSERT INTO menu_items (item_id, category_id, name, description, price_inr, vegetarian, vegan, contains_egg, allergens, available) VALUES (3, 1, 'Vegetable Samosa', 'Crisp pastry filled with spiced vegetables.', 220.0, 1, 1, 0, 'Gluten', 1);
INSERT INTO menu_items (item_id, category_id, name, description, price_inr, vegetarian, vegan, contains_egg, allergens, available) VALUES (4, 2, 'Butter Chicken', 'Creamy tomato chicken curry with basmati rice.', 520.0, 0, 0, 0, 'Dairy', 1);
INSERT INTO menu_items (item_id, category_id, name, description, price_inr, vegetarian, vegan, contains_egg, allergens, available) VALUES (5, 2, 'Fish Curry', 'South Indian style fish curry with steamed rice.', 560.0, 0, 0, 0, 'Fish', 1);
INSERT INTO menu_items (item_id, category_id, name, description, price_inr, vegetarian, vegan, contains_egg, allergens, available) VALUES (6, 3, 'Paneer Butter Masala', 'Paneer in tomato and cashew gravy.', 480.0, 1, 0, 0, 'Dairy, Nuts', 1);
INSERT INTO menu_items (item_id, category_id, name, description, price_inr, vegetarian, vegan, contains_egg, allergens, available) VALUES (7, 3, 'Vegetable Biryani', 'Fragrant basmati rice with seasonal vegetables.', 390.0, 1, 1, 0, '', 1);
INSERT INTO menu_items (item_id, category_id, name, description, price_inr, vegetarian, vegan, contains_egg, allergens, available) VALUES (8, 4, 'Chocolate Brownie', 'Warm brownie with vanilla ice cream.', 280.0, 1, 0, 1, 'Dairy, Egg, Gluten', 1);
INSERT INTO menu_items (item_id, category_id, name, description, price_inr, vegetarian, vegan, contains_egg, allergens, available) VALUES (9, 4, 'Fresh Fruit Bowl', 'Seasonal cut fruit.', 220.0, 1, 1, 0, '', 1);
INSERT INTO menu_items (item_id, category_id, name, description, price_inr, vegetarian, vegan, contains_egg, allergens, available) VALUES (10, 5, 'Masala Chai', 'Indian spiced tea.', 140.0, 1, 1, 0, '', 1);
INSERT INTO menu_items (item_id, category_id, name, description, price_inr, vegetarian, vegan, contains_egg, allergens, available) VALUES (11, 5, 'Fresh Lime Soda', 'Fresh lime with soda, sweet or salted.', 160.0, 1, 1, 0, '', 1);
INSERT INTO menu_items (item_id, category_id, name, description, price_inr, vegetarian, vegan, contains_egg, allergens, available) VALUES (12, 5, 'Mineral Water', 'Still bottled water.', 80.0, 1, 1, 0, '', 1);
-- hotel_services
INSERT INTO hotel_services (service_id, name, description, availability, phone_extension) VALUES (1, 'Room Service', 'Food and drinks delivered to a guest room.', '06:00-23:00', '101');
INSERT INTO hotel_services (service_id, name, description, availability, phone_extension) VALUES (2, 'Housekeeping', 'Cleaning, towels and room amenities.', '08:00-22:00', '102');
INSERT INTO hotel_services (service_id, name, description, availability, phone_extension) VALUES (3, 'Maintenance', 'Repairs and technical assistance.', '24 hours', '103');
INSERT INTO hotel_services (service_id, name, description, availability, phone_extension) VALUES (4, 'Front Desk', 'General hotel assistance and guest support.', '24 hours', '100');
INSERT INTO hotel_services (service_id, name, description, availability, phone_extension) VALUES (5, 'Wake-up Call', 'Scheduled wake-up calls.', '24 hours', '100');
INSERT INTO hotel_services (service_id, name, description, availability, phone_extension) VALUES (6, 'Luggage Assistance', 'Help with luggage on arrival or departure.', '06:00-23:00', '104');
-- service_requests
-- restaurant_orders
INSERT INTO restaurant_orders (order_id, guest_id, room_id, order_source, status, created_at) VALUES (5001, 1, 10, 'room_service', 'served', '2026-09-08T08:15:00');
INSERT INTO restaurant_orders (order_id, guest_id, room_id, order_source, status, created_at) VALUES (5002, 2, 2, 'room_service', 'preparing', '2026-09-08T08:30:00');
-- restaurant_order_items
INSERT INTO restaurant_order_items (order_item_id, order_id, item_id, quantity, unit_price_inr) VALUES (1, 5001, 2, 1, 360.0);
INSERT INTO restaurant_order_items (order_item_id, order_id, item_id, quantity, unit_price_inr) VALUES (2, 5001, 10, 2, 140.0);
INSERT INTO restaurant_order_items (order_item_id, order_id, item_id, quantity, unit_price_inr) VALUES (3, 5002, 4, 1, 520.0);
INSERT INTO restaurant_order_items (order_item_id, order_id, item_id, quantity, unit_price_inr) VALUES (4, 5002, 12, 1, 80.0);
COMMIT;
PRAGMA foreign_keys=ON;
