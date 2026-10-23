package config

import (
	"reflect"
)

// applyOverride merges a pointer-based override struct into a value-based config struct.
// For each field in the override struct: if the field is a non-nil pointer whose name
// matches a field in the config struct, the pointed-to value is assigned to the config field.
//
// Both dst and ov must be pointers to structs.
func applyOverride(dst, ov interface{}) {
	dstVal := reflect.ValueOf(dst).Elem()
	ovVal := reflect.ValueOf(ov).Elem()
	ovType := ovVal.Type()

	for i := 0; i < ovVal.NumField(); i++ {
		ovField := ovVal.Field(i)
		fieldName := ovType.Field(i).Name

		if ovField.Kind() != reflect.Ptr || ovField.IsNil() {
			continue
		}

		dstField := dstVal.FieldByName(fieldName)
		if !dstField.IsValid() || !dstField.CanSet() {
			continue
		}

		dstField.Set(ovField.Elem())
	}
}
