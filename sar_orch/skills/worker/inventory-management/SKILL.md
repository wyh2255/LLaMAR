---
name: inventory-management
description: Managing your 3-slot inventory — supply collection, person carrying constraints, clearing, and planning.
---

# Skill: Inventory Management

How to manage your limited 3-slot inventory.

## Capacity Rules

- **3 slots total**: can hold Sand, Water, or a Person
- **Person occupies ALL 3 slots** — carrying a person means you drop all other resources
- When you pick up a person, your inventory becomes `['Person']` and everything else is lost

## Supply Collection

- `get_supply(reservoir_id="...")` collects 1 unit per step
- You can hold up to 3 units of supplies (any mix of Sand and Water)
- Check reservoir type before collecting — Sand reservoir gives Sand, Water reservoir gives Water

## Person Carrying

Before carrying a person:
- Clear your inventory first by dropping supplies at a deposit, or using them on fires
- Call `carry_person(person_id="...")` when at the person's position
- After picking up, your inventory shows `['Person']` (3 slots used)

## Clearing Inventory

- `clear_inventory()` discards ALL items at your current position
- Use before picking up a person if you have supplies you can't store
- Better: use remaining supplies on nearby fires before clearing

## Planning

- Check your inventory in Environment State before deciding next action
- If you have supplies but no nearby fires, conserve them — don't clear unless you need to carry a person
- If you're low on supplies and near a reservoir, restock before moving to the next fire
