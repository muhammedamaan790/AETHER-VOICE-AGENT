# AETHER Hotel Demo Database

Synthetic SQLite database for the AETHER hotel voice-manager demo.

## Inventory
- 1 hotel
- **50 rooms** across 5 floors
- 5 room types
- Synthetic guests, reservations, menu items, hotel services, service requests, and restaurant orders
- No real personal data

## Room inventory
- Standard King
- Standard Twin
- Deluxe King
- Executive Suite
- Family Suite

Existing reservation-linked rooms were preserved; the inventory was expanded to exactly 50 rooms.

## Views
- `available_rooms`
- `available_menu`
- `current_reservations`

The database is intended to be queried by AETHER so room/menu/service answers come from data rather than model guesses.
